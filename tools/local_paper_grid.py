#!/usr/bin/env python3
"""Local paper spot-grid fill engine — venue=local_paper. NOT OKX Bot equity.

Replays a price path (synthetic / CSV / OKX *public* market candles) through an
arithmetic spot grid and emits the team sim-daily-schema metrics block.

Hard gates (paper only):
  * no secrets, no private OKX API, no order / amend / withdraw HTTP;
  * the only network call is an optional read-only GET of public market candles;
  * spot, lever=1, no averaging down, no auto-restart after SL.

Fill model (deliberately simple, documented in README):
  * gridNum arithmetic intervals between minPx and maxPx (gridNum+1 price lines);
  * investment split evenly per grid; buy limit at the lower line, sell limit at
    the upper line of each grid; grids above the start price are bought at
    market on start (taker), grids below wait with a buy limit;
  * each candle is walked open→low→high→close (or open→high→low→close when the
    candle closes below its open); fills happen exactly at grid lines;
  * fees: worst-case (every fill at max(maker, taker)) by default, or maker
    for limit fills / taker for market fills; fees are deducted from quote cash;
  * slTriggerPx: when a candle's low touches it, all inventory is sold at the
    trigger price (optionally minus slippage) and the bot stops. No restart.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
VENUE = "local_paper"
DISCLAIMER = (
    "本地 paper 成交模拟 ≠ OKX Bot 净值 / ≠ 收益承诺；venue=local_paper；"
    "零下单、零 amend、零密钥；仅供团队观察网格假设。"
)
OKX_PUBLIC_BASE = "https://www.okx.com"
OKX_CANDLES_PATH = "/api/v5/market/candles"
PNL_RATIO_INVALIDATION = -0.08
DEFAULT_RUN_ID = "local-paper-eth-grid-001"

SIM_ASSUMPTIONS = [
    "算术网格：gridNum 个区间，gridNum+1 条格线；每格等额 investment/gridNum",
    "限价成交只发生在格线上；同一根 K 线内按 open→low→high→close（或反向）顺序撮合",
    "起始价上方的格子在启动时按市价（taker）一次性买入；下方格子挂买单等待",
    "起始价不在区间内则等待首根收盘价进入区间后再启动（不追单）",
    "手续费默认 worst-case：每笔按 max(maker, taker)，从 quote 现金扣除"
    "（OKX 买入手续费实际扣 base，这里是纸面简化）",
    "无滑点、无最小下单量 / 步长约束、无部分成交、无订单簿深度",
    "slTriggerPx：K 线 low 触及即按触发价（可加滑点）清仓并停机，不自动重启、不摊平",
    "okx_bot_total_pnl = realized + unrealized（费前）；"
    "fee_after_pnl_est = 现金记账权益 − investment；二者差值恒等于 fees_paid_est",
]


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Candle:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float

    def path(self) -> list[float]:
        """Intra-candle price path approximation (backtest convention)."""
        if self.close >= self.open:
            pts = [self.open, self.low, self.high, self.close]
        else:
            pts = [self.open, self.high, self.low, self.close]
        out: list[float] = []
        for p in pts:
            if not out or out[-1] != p:
                out.append(p)
        return out


@dataclass(frozen=True)
class GridParams:
    min_px: float
    max_px: float
    grid_num: int
    investment: float
    sl_trigger_px: float | None = 2150.0
    tp_trigger_px: float | None = None
    lever: int = 1

    def validate(self) -> None:
        if self.grid_num <= 0 or self.min_px <= 0 or self.max_px <= self.min_px:
            raise ValueError("need grid_num>0 and max_px>min_px>0")
        if self.investment <= 0:
            raise ValueError("investment must be > 0")
        if self.lever != 1:
            raise ValueError("local_paper is spot 1x only (lever must be 1)")
        if self.sl_trigger_px is not None and self.sl_trigger_px >= self.min_px:
            raise ValueError("slTriggerPx must be below minPx (absolute price)")
        if self.tp_trigger_px is not None and self.tp_trigger_px <= self.max_px:
            raise ValueError("tpTriggerPx must be above maxPx (absolute price)")

    @property
    def step(self) -> float:
        return (self.max_px - self.min_px) / self.grid_num

    @property
    def per_grid_pct(self) -> float:
        """Same convention as tools/grid_fee_sensitivity.py (step / mid, grid_num intervals)."""
        return self.step / ((self.min_px + self.max_px) / 2.0)

    @property
    def per_grid_quote(self) -> float:
        return self.investment / self.grid_num

    def to_schema(self) -> dict:
        return {
            "minPx": self.min_px,
            "maxPx": self.max_px,
            "gridNum": self.grid_num,
            "investment": self.investment,
            "lever": self.lever,
            "slTriggerPx": self.sl_trigger_px,
            "tpTriggerPx": self.tp_trigger_px,
        }


@dataclass(frozen=True)
class FeeModel:
    maker: float = 0.0008
    taker: float = 0.0010
    worst_case: bool = True

    def rate(self, liquidity: str) -> float:
        if self.worst_case:
            return max(self.maker, self.taker)
        return self.taker if liquidity == "taker" else self.maker

    @property
    def round_trip(self) -> float:
        if self.worst_case:
            return 2.0 * max(self.maker, self.taker)
        return 2.0 * self.maker  # grid legs are both limit fills

    def to_dict(self) -> dict:
        return {
            "maker": self.maker,
            "taker": self.taker,
            "mode": "worst_case_max_each_fill" if self.worst_case else "maker_limit_taker_market",
            "round_trip_paper": self.round_trip,
        }


@dataclass
class Fill:
    ts_ms: int
    side: str  # buy | sell
    kind: str  # initial_buy | grid_buy | grid_sell | sl_sell | tp_sell
    px: float
    qty: float
    notional: float
    fee_quote: float
    grid_idx: int  # -1 for aggregated fills
    liquidity: str  # maker | taker


@dataclass
class GridSlot:
    idx: int
    lower: float
    upper: float
    holding: bool = False
    qty: float = 0.0
    cost_px: float = 0.0


def build_levels(params: GridParams) -> list[float]:
    step = params.step
    levels = [params.min_px + i * step for i in range(params.grid_num + 1)]
    levels[-1] = params.max_px
    return levels


# --------------------------------------------------------------------------- engine


@dataclass
class EngineState:
    started: bool = False
    stopped: bool = False
    stop_reason: str | None = None
    started_ts_ms: int | None = None
    quote_cash: float = 0.0
    base_qty: float = 0.0
    fees_quote: float = 0.0
    grid_profit: float = 0.0
    realized_exit: float = 0.0  # SL / TP liquidation vs cost, pre-fee
    initial_buy_qty: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    arbitrage_num: int = 0
    sl_hit_count: int = 0
    tp_hit_count: int = 0
    last_px: float | None = None
    last_ts_ms: int | None = None
    first_ts_ms: int | None = None
    candles: int = 0
    peak_ratio: float = 0.0
    max_drawdown_ratio: float = 0.0
    min_pnl_ratio: float = 0.0
    quote_cash_min: float = 0.0
    below_min_px_candles: int = 0
    above_max_px_candles: int = 0
    invalidation_events: list[dict] = field(default_factory=list)
    flags_ever: set[str] = field(default_factory=set)


class LocalPaperGrid:
    def __init__(self, params: GridParams, fees: FeeModel, sl_slippage_bps: float = 0.0):
        params.validate()
        self.params = params
        self.fees = fees
        self.sl_slippage_bps = sl_slippage_bps
        self.levels = build_levels(params)
        self.slots = [
            GridSlot(idx=i, lower=self.levels[i], upper=self.levels[i + 1])
            for i in range(params.grid_num)
        ]
        self.fills: list[Fill] = []
        self.s = EngineState(quote_cash=params.investment, quote_cash_min=params.investment)
        if fees.round_trip >= params.per_grid_pct:
            self._hit_flag("fee_gte_per_grid", None, params.min_px)

    # ---- helpers

    def _hit_flag(self, name: str, ts_ms: int | None, px: float) -> None:
        if name in self.s.flags_ever:
            return
        self.s.flags_ever.add(name)
        self.s.invalidation_events.append(
            {"flag": name, "ts": fmt_cst(ts_ms) if ts_ms is not None else None, "px": px}
        )

    def _record(self, fill: Fill) -> None:
        self.fills.append(fill)
        if fill.side == "buy":
            self.s.buy_count += 1
        else:
            self.s.sell_count += 1
        self.s.fees_quote += fill.fee_quote
        self.s.quote_cash_min = min(self.s.quote_cash_min, self.s.quote_cash)

    def _in_band(self, px: float) -> bool:
        return self.params.min_px <= px <= self.params.max_px

    # ---- lifecycle

    def start(self, px: float, ts_ms: int) -> None:
        """Place the grid around px. Grids whose lower line is >= px are bought at market."""
        self.s.started = True
        self.s.started_ts_ms = ts_ms
        pgq = self.params.per_grid_quote
        holding = [slot for slot in self.slots if slot.lower >= px]
        if not holding:
            return
        notional = pgq * len(holding)
        qty_each = pgq / px
        rate = self.fees.rate("taker")
        fee = notional * rate
        for slot in holding:
            slot.holding = True
            slot.qty = qty_each
            slot.cost_px = px
        self.s.quote_cash -= notional + fee
        qty_total = qty_each * len(holding)
        self.s.initial_buy_qty = qty_total
        self.s.base_qty += qty_total
        self._record(Fill(ts_ms, "buy", "initial_buy", px, qty_total, notional, fee, -1, "taker"))

    def _grid_buy(self, slot: GridSlot, ts_ms: int) -> None:
        pgq = self.params.per_grid_quote
        px = slot.lower
        qty = pgq / px
        rate = self.fees.rate("maker")
        fee = pgq * rate
        slot.holding = True
        slot.qty = qty
        slot.cost_px = px
        self.s.quote_cash -= pgq + fee
        self.s.base_qty += qty
        self._record(Fill(ts_ms, "buy", "grid_buy", px, qty, pgq, fee, slot.idx, "maker"))

    def _grid_sell(self, slot: GridSlot, ts_ms: int) -> None:
        px = slot.upper
        qty = slot.qty
        proceeds = qty * px
        rate = self.fees.rate("maker")
        fee = proceeds * rate
        self.s.quote_cash += proceeds - fee
        self.s.base_qty -= qty
        self.s.grid_profit += qty * (px - slot.cost_px)
        self.s.arbitrage_num += 1
        slot.holding = False
        slot.qty = 0.0
        slot.cost_px = 0.0
        self._record(Fill(ts_ms, "sell", "grid_sell", px, qty, proceeds, fee, slot.idx, "maker"))

    def _liquidate(self, px: float, ts_ms: int, kind: str) -> None:
        qty = sum(slot.qty for slot in self.slots if slot.holding)
        if qty > 0:
            proceeds = qty * px
            fee = proceeds * self.fees.rate("taker")
            for slot in self.slots:
                if slot.holding:
                    self.s.realized_exit += slot.qty * (px - slot.cost_px)
                    slot.holding = False
                    slot.qty = 0.0
                    slot.cost_px = 0.0
            self.s.quote_cash += proceeds - fee
            self.s.base_qty = 0.0
            self._record(Fill(ts_ms, "sell", kind, px, qty, proceeds, fee, -1, "taker"))
        self.s.stopped = True
        self.s.stop_reason = kind

    def _segment_down(self, a: float, b: float, ts_ms: int) -> bool:
        """Price falls from a to b. Fill waiting buys at lines in [b, a). Returns True if SL hit."""
        sl = self.params.sl_trigger_px
        floor = b
        sl_hit = sl is not None and b <= sl
        if sl_hit:
            floor = sl
        for slot in reversed(self.slots):
            if not slot.holding and floor <= slot.lower < a:
                self._grid_buy(slot, ts_ms)
        if sl_hit:
            assert sl is not None
            exit_px = sl * (1.0 - self.sl_slippage_bps / 10_000.0)
            self._liquidate(exit_px, ts_ms, "sl_sell")
            self.s.sl_hit_count += 1
            self._hit_flag("sl_triggered", ts_ms, sl)
            return True
        return False

    def _segment_up(self, a: float, b: float, ts_ms: int) -> bool:
        """Price rises from a to b. Fill sells at lines in (a, b]. Returns True if TP hit."""
        tp = self.params.tp_trigger_px
        ceil = b
        tp_hit = tp is not None and b >= tp
        if tp_hit:
            ceil = tp
        for slot in self.slots:
            if slot.holding and a < slot.upper <= ceil:
                self._grid_sell(slot, ts_ms)
        if tp_hit:
            assert tp is not None
            self._liquidate(tp, ts_ms, "tp_sell")
            self.s.tp_hit_count += 1
            return True
        return False

    # ---- public

    def process(self, c: Candle) -> None:
        s = self.s
        if s.first_ts_ms is None:
            s.first_ts_ms = c.ts_ms
        s.candles += 1
        if c.close < self.params.min_px:
            s.below_min_px_candles += 1
        elif c.close > self.params.max_px:
            s.above_max_px_candles += 1

        if not s.stopped:
            if not s.started:
                if self._in_band(c.close):
                    self.start(c.close, c.ts_ms)
            else:
                pts = c.path()
                for a, b in zip(pts, pts[1:]):
                    if b < a:
                        if self._segment_down(a, b, c.ts_ms):
                            break
                    elif b > a:
                        if self._segment_up(a, b, c.ts_ms):
                            break

        s.last_px = c.close
        s.last_ts_ms = c.ts_ms
        self._mark(c)

    def _mark(self, c: Candle) -> None:
        s = self.s
        ratio = self.total_pnl() / self.params.investment
        s.peak_ratio = max(s.peak_ratio, ratio)
        s.max_drawdown_ratio = max(s.max_drawdown_ratio, s.peak_ratio - ratio)
        s.min_pnl_ratio = min(s.min_pnl_ratio, ratio)
        if c.close < self.params.min_px:
            self._hit_flag("below_min_px", c.ts_ms, c.close)
        if ratio <= PNL_RATIO_INVALIDATION:
            self._hit_flag("pnl_ratio_lte_minus_8pct", c.ts_ms, c.close)

    def run(self, candles: list[Candle]) -> None:
        for c in candles:
            self.process(c)

    # ---- accounting

    def unrealized_pnl(self) -> float:
        px = self.s.last_px
        if px is None:
            return 0.0
        return sum(slot.qty * (px - slot.cost_px) for slot in self.slots if slot.holding)

    def realized_pnl(self) -> float:
        return self.s.grid_profit + self.s.realized_exit

    def total_pnl(self) -> float:
        return self.realized_pnl() + self.unrealized_pnl()

    def equity(self) -> float:
        px = self.s.last_px or 0.0
        return self.s.quote_cash + self.s.base_qty * px

    def fee_after_pnl(self) -> float:
        return self.equity() - self.params.investment

    def metrics(self) -> dict:
        s = self.s
        inv = self.params.investment
        total = self.total_pnl()
        unreal = self.unrealized_pnl()
        return {
            "okx_bot_total_pnl": round(total, 6),
            "okx_bot_total_pnl_ratio": round(total / inv, 8),
            "realized_pnl": round(self.realized_pnl(), 6),
            "unrealized_pnl": round(unreal, 6),
            "grid_profit": round(s.grid_profit, 6),
            "float_profit": round(unreal, 6),
            "fees_paid_est": round(s.fees_quote, 6),
            "fee_after_pnl_est": round(self.fee_after_pnl(), 6),
            "arbitrage_num": s.arbitrage_num,
            "buy_count": s.buy_count,
            "sell_count": s.sell_count,
            "sl_hit_count": s.sl_hit_count,
            "invalidation_hit_count": len(s.invalidation_events),
            "max_drawdown_ratio": round(s.max_drawdown_ratio, 8),
            "last_px": s.last_px,
            # paper extras (schema allows additional keys; bootstrap file used these too)
            "per_grid_pct_paper": self.params.per_grid_pct,
            "fee_round_trip_paper": self.fees.round_trip,
            "fee_falsified": self.fees.round_trip >= self.params.per_grid_pct,
            "fee_after_pnl_ratio_est": round(self.fee_after_pnl() / inv, 8),
            "min_pnl_ratio": round(s.min_pnl_ratio, 8),
            "equity_est": round(self.equity(), 6),
            "quote_cash_est": round(s.quote_cash, 6),
            "quote_cash_min_est": round(s.quote_cash_min, 6),
            "base_inventory_est": round(s.base_qty, 10),
            "initial_buy_qty": round(s.initial_buy_qty, 10),
            "tp_hit_count": s.tp_hit_count,
            "candles": s.candles,
            "below_min_px_candles": s.below_min_px_candles,
            "above_max_px_candles": s.above_max_px_candles,
            "started_at": fmt_cst(s.started_ts_ms) if s.started_ts_ms is not None else None,
            "stop_reason": s.stop_reason,
        }

    def invalidation_flags(self) -> dict:
        s = self.s
        px = s.last_px
        ratio = self.total_pnl() / self.params.investment
        return {
            "below_min_px": px is not None and px < self.params.min_px,
            "sl_triggered": s.sl_hit_count > 0,
            "pnl_ratio_lte_minus_8pct": ratio <= PNL_RATIO_INVALIDATION,
            "fee_gte_per_grid": self.fees.round_trip >= self.params.per_grid_pct,
            "price_in_band": px is not None and self._in_band(px),
            "below_min_px_ever": "below_min_px" in s.flags_ever,
            "pnl_ratio_lte_minus_8pct_ever": "pnl_ratio_lte_minus_8pct" in s.flags_ever,
        }


# --------------------------------------------------------------------------- time


def fmt_cst(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=SHANGHAI).strftime("%Y-%m-%d %H:%M:%S CST")


def parse_cst(text: str) -> int:
    """'YYYY-MM-DD[ HH:MM[:SS]]' in Asia/Shanghai (or ISO with offset) → epoch ms."""
    text = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return int(dt.timestamp() * 1000)


def parse_ts_cell(cell: str) -> int:
    cell = cell.strip()
    if cell.lstrip("-").replace(".", "", 1).isdigit():
        val = float(cell)
        return int(val if val > 1e11 else val * 1000.0)  # seconds vs milliseconds
    return parse_cst(cell)


BAR_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H": 3_600_000,
    "2H": 7_200_000,
    "4H": 14_400_000,
    "1D": 86_400_000,
}


# --------------------------------------------------------------------------- sources


def gen_synthetic(
    start_px: float,
    amplitude: float,
    period_steps: int,
    steps: int,
    noise: float,
    seed: int,
    drift_per_step: float = 0.0,
    start_ts_ms: int | None = None,
    bar_ms: int = 60_000,
) -> list[Candle]:
    """Deterministic oscillating path: sine + optional drift + gaussian noise."""
    if steps <= 0 or period_steps <= 0:
        raise ValueError("steps and period must be > 0")
    rng = random.Random(seed)
    if start_ts_ms is None:
        start_ts_ms = parse_cst(datetime.now(SHANGHAI).strftime("%Y-%m-%d"))
    prices = []
    for i in range(steps + 1):
        centre = start_px + drift_per_step * i
        px = centre + amplitude * math.sin(2.0 * math.pi * i / period_steps)
        if noise > 0:
            px += rng.gauss(0.0, noise)
        prices.append(max(px, 0.01))
    out: list[Candle] = []
    for i in range(steps):
        o, c = prices[i], prices[i + 1]
        wick = abs(rng.gauss(0.0, noise)) if noise > 0 else 0.0
        hi = max(o, c) + wick
        lo = max(min(o, c) - wick, 0.01)
        out.append(Candle(start_ts_ms + i * bar_ms, o, hi, lo, c))
    return out


def load_csv(path: str) -> list[Candle]:
    """CSV with header (ts/open/high/low/close, extra columns ignored) or headerless
    positional rows ts,open,high,low,close[,...] (OKX raw row shape works)."""
    aliases = {
        "ts": ("ts", "timestamp", "time", "date", "datetime", "open_time"),
        "open": ("open", "o"),
        "high": ("high", "h"),
        "low": ("low", "l"),
        "close": ("close", "c"),
    }
    out: list[Candle] = []
    with open(path, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.reader(f) if r and any(cell.strip() for cell in r)]
    if not rows:
        return out
    first = [c.strip().lower() for c in rows[0]]
    has_header = not first[0].lstrip("-").replace(".", "", 1).isdigit()
    if has_header:
        idx: dict[str, int] = {}
        for key, names in aliases.items():
            for name in names:
                if name in first:
                    idx[key] = first.index(name)
                    break
            if key not in idx:
                raise ValueError(f"CSV missing column for {key}; header={rows[0]}")
        body = rows[1:]
    else:
        idx = {"ts": 0, "open": 1, "high": 2, "low": 3, "close": 4}
        body = rows
    for r in body:
        out.append(
            Candle(
                parse_ts_cell(r[idx["ts"]]),
                float(r[idx["open"]]),
                float(r[idx["high"]]),
                float(r[idx["low"]]),
                float(r[idx["close"]]),
            )
        )
    out.sort(key=lambda c: c.ts_ms)
    return out


def save_csv(path: str, candles: list[Candle]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "ts_cst"])
        for c in candles:
            w.writerow([c.ts_ms, c.open, c.high, c.low, c.close, fmt_cst(c.ts_ms)])


def fetch_okx_public_candles(
    inst_id: str,
    bar: str = "1m",
    limit: int = 300,
    pages: int = 1,
    base_url: str = OKX_PUBLIC_BASE,
    timeout: float = 10.0,
) -> tuple[list[Candle], dict]:
    """Read-only GET of OKX public market candles. No auth, no signing, no trade endpoints.

    Pagination: `after` = oldest ts of the previous page → older records.
    """
    if not (1 <= limit <= 300):
        raise ValueError("OKX market/candles limit must be 1..300")
    candles: list[Candle] = []
    after: str | None = None
    requests_made = 0
    for _ in range(max(pages, 1)):
        q = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after:
            q["after"] = after
        url = f"{base_url}{OKX_CANDLES_PATH}?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(
            url, method="GET", headers={"User-Agent": "cryptowang-local-paper/0.1 (read-only)"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 public GET
            payload = json.loads(resp.read().decode("utf-8"))
        requests_made += 1
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX public API error: {payload.get('code')} {payload.get('msg')}")
        rows = payload.get("data") or []
        if not rows:
            break
        for r in rows:
            candles.append(Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        after = rows[-1][0]
        if len(rows) < limit:
            break
    dedup = {c.ts_ms: c for c in candles}
    ordered = sorted(dedup.values(), key=lambda c: c.ts_ms)
    meta = {
        "kind": "okx_public_market_candles",
        "endpoint": f"GET {base_url}{OKX_CANDLES_PATH}",
        "auth": "none",
        "read_only": True,
        "instId": inst_id,
        "bar": bar,
        "limit": limit,
        "pages": pages,
        "requests_made": requests_made,
    }
    return ordered, meta


def clip_window(candles: list[Candle], from_ms: int | None, to_ms: int | None) -> list[Candle]:
    return [
        c
        for c in candles
        if (from_ms is None or c.ts_ms >= from_ms) and (to_ms is None or c.ts_ms <= to_ms)
    ]


# --------------------------------------------------------------------------- report


def build_report(
    engine: LocalPaperGrid,
    *,
    run_id: str,
    inst_id: str,
    source_kind: str,
    data_source: dict,
    window_label: str,
    notes: str = "",
    include_fills: int = 0,
) -> dict:
    s = engine.s
    metrics = engine.metrics()
    flags = engine.invalidation_flags()
    auto_notes = (
        f"local_paper sim over {s.candles} candles ({source_kind}); "
        f"fills buy={s.buy_count} sell={s.sell_count} arb={s.arbitrage_num}; "
        f"stop_reason={s.stop_reason or 'none'}. "
        "Paper ≠ OKX Bot equity. Not a performance claim."
    )
    fills_by_kind: dict[str, int] = {}
    for f in engine.fills:
        fills_by_kind[f.kind] = fills_by_kind.get(f.kind, 0) + 1
    doc = {
        "pulled_at": datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S CST"),
        "source": f"local_paper_sim:{source_kind}",
        "mode": "模拟",
        "venue": VENUE,
        "runId": run_id,
        "algoId": None,
        "strategy": "okx_spot_grid",
        "instId": inst_id,
        "params": engine.params.to_schema(),
        "window": {
            "from": fmt_cst(s.first_ts_ms),
            "to": fmt_cst(s.last_ts_ms),
            "label": window_label,
        },
        "metrics": metrics,
        "invalidation_flags": flags,
        "invalidation_events": s.invalidation_events,
        "notes": (auto_notes + (" " + notes if notes else "")).strip(),
        "disclaimer": DISCLAIMER,
        "will_send_http": False,
        "trading_http": {
            "will_send_http": False,
            "method": "NONE",
            "order": False,
            "amend": False,
            "withdraw": False,
            "policy": "only optional read-only GET of public market candles; no trade endpoints",
        },
        "data_source": data_source,
        "fee_model": engine.fees.to_dict(),
        "grid_levels": {
            "count": len(engine.levels),
            "step": engine.params.step,
            "first": engine.levels[0],
            "last": engine.levels[-1],
        },
        "fills_by_kind": fills_by_kind,
        "sim_assumptions": SIM_ASSUMPTIONS,
    }
    if include_fills:
        doc["fills_preview"] = [
            {**asdict(f), "ts": fmt_cst(f.ts_ms)} for f in engine.fills[: max(include_fills, 0)]
        ]
    return doc


def write_fills_csv(path: str, fills: list[Fill]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "ts_ms",
                "ts_cst",
                "side",
                "kind",
                "px",
                "qty",
                "notional",
                "fee_quote",
                "grid_idx",
                "liquidity",
            ]
        )
        for x in fills:
            w.writerow(
                [
                    x.ts_ms,
                    fmt_cst(x.ts_ms),
                    x.side,
                    x.kind,
                    x.px,
                    x.qty,
                    x.notional,
                    x.fee_quote,
                    x.grid_idx,
                    x.liquidity,
                ]
            )


def default_out_name(doc: dict) -> str:
    to = (doc.get("window") or {}).get("to") or datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    return f"{to[:10]}-{doc['runId']}.json"


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Local paper spot-grid fill simulator (venue=local_paper). "
        "Paper ≠ OKX Bot equity; not a performance promise. No orders, no amend, no secrets.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    g = p.add_argument_group("grid params (demo ETH-USDT)")
    g.add_argument("--inst-id", default="ETH-USDT")
    g.add_argument("--min-px", type=float, default=2200.0)
    g.add_argument("--max-px", type=float, default=3200.0)
    g.add_argument("--grid-num", type=int, default=30)
    g.add_argument("--investment", type=float, default=1000.0)
    g.add_argument("--sl-trigger-px", type=float, default=2150.0, help="absolute price")
    g.add_argument("--no-sl", action="store_true", help="disable slTriggerPx")
    g.add_argument("--tp-trigger-px", type=float, default=None, help="absolute price (optional)")

    f = p.add_argument_group("fees")
    f.add_argument("--maker", type=float, default=0.0008)
    f.add_argument("--taker", type=float, default=0.0010)
    f.add_argument(
        "--fee-mode",
        choices=["worst_case", "maker_taker"],
        default="worst_case",
        help="worst_case: every fill at max(maker,taker); maker_taker: limit=maker, market=taker",
    )
    f.add_argument("--sl-slippage-bps", type=float, default=0.0)

    src = p.add_argument_group("price path")
    src.add_argument("--source", choices=["synthetic", "csv", "okx-public"], default="synthetic")
    src.add_argument("--candles-csv", default="", help="CSV path for --source csv")
    src.add_argument("--bar", default="1m", help="OKX bar for --source okx-public")
    src.add_argument("--limit", type=int, default=300, help="per-request candles (max 300)")
    src.add_argument("--pages", type=int, default=5, help="pages to walk back (300*5 ≈ 25h @1m)")
    src.add_argument("--save-candles-csv", default="", help="dump ingested candles for replay")
    src.add_argument("--synthetic-start", type=float, default=2700.0)
    src.add_argument("--synthetic-amp", type=float, default=250.0)
    src.add_argument("--synthetic-period", type=int, default=240, help="steps per full cycle")
    src.add_argument("--synthetic-steps", type=int, default=1440, help="1440 × 1m = one day")
    src.add_argument("--synthetic-noise", type=float, default=3.0)
    src.add_argument("--synthetic-drift", type=float, default=0.0, help="price per step")
    src.add_argument("--synthetic-start-ts", default="", help="CST 'YYYY-MM-DD HH:MM'")
    src.add_argument("--seed", type=int, default=20260916)

    w = p.add_argument_group("window / output")
    w.add_argument("--window-from", default="", help="CST 'YYYY-MM-DD HH:MM' (clip candles)")
    w.add_argument("--window-to", default="", help="CST 'YYYY-MM-DD HH:MM' (clip candles)")
    w.add_argument("--window-label", default="", help="eod|intraday|synthetic (auto)")
    w.add_argument("--run-id", default=DEFAULT_RUN_ID)
    w.add_argument("--notes", default="")
    w.add_argument("--out", default="", help="write JSON to this path")
    w.add_argument("--out-dir", default="", help="write JSON as <dir>/YYYY-MM-DD-<runId>.json")
    w.add_argument("--fills-out", default="", help="write all fills as CSV")
    w.add_argument("--include-fills", type=int, default=0, help="embed first N fills in JSON")
    w.add_argument("--quiet", action="store_true", help="do not print JSON to stdout")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    params = GridParams(
        min_px=args.min_px,
        max_px=args.max_px,
        grid_num=args.grid_num,
        investment=args.investment,
        sl_trigger_px=None if args.no_sl else args.sl_trigger_px,
        tp_trigger_px=args.tp_trigger_px,
        lever=1,
    )
    fees = FeeModel(maker=args.maker, taker=args.taker, worst_case=args.fee_mode == "worst_case")

    if args.source == "synthetic":
        start_ts = parse_cst(args.synthetic_start_ts) if args.synthetic_start_ts else None
        candles = gen_synthetic(
            args.synthetic_start,
            args.synthetic_amp,
            args.synthetic_period,
            args.synthetic_steps,
            args.synthetic_noise,
            args.seed,
            drift_per_step=args.synthetic_drift,
            start_ts_ms=start_ts,
        )
        data_source = {
            "kind": "synthetic_oscillating",
            "http_fetch": False,
            "start_px": args.synthetic_start,
            "amplitude": args.synthetic_amp,
            "period_steps": args.synthetic_period,
            "steps": args.synthetic_steps,
            "noise": args.synthetic_noise,
            "drift_per_step": args.synthetic_drift,
            "seed": args.seed,
        }
        label = args.window_label or "synthetic"
    elif args.source == "csv":
        if not args.candles_csv:
            print("--candles-csv is required for --source csv", file=sys.stderr)
            return 2
        candles = load_csv(args.candles_csv)
        data_source = {"kind": "csv", "http_fetch": False, "path": args.candles_csv}
        label = args.window_label or "eod"
    else:
        candles, meta = fetch_okx_public_candles(
            args.inst_id, bar=args.bar, limit=args.limit, pages=args.pages
        )
        data_source = {**meta, "http_fetch": True}
        label = args.window_label or "eod"

    from_ms = parse_cst(args.window_from) if args.window_from else None
    to_ms = parse_cst(args.window_to) if args.window_to else None
    candles = clip_window(candles, from_ms, to_ms)
    data_source["candles"] = len(candles)
    if candles:
        data_source["first_ts"] = fmt_cst(candles[0].ts_ms)
        data_source["last_ts"] = fmt_cst(candles[-1].ts_ms)
    if args.save_candles_csv:
        save_csv(args.save_candles_csv, candles)
        data_source["saved_csv"] = args.save_candles_csv

    engine = LocalPaperGrid(params, fees, sl_slippage_bps=args.sl_slippage_bps)
    engine.run(candles)

    doc = build_report(
        engine,
        run_id=args.run_id,
        inst_id=args.inst_id,
        source_kind=args.source,
        data_source=data_source,
        window_label=label,
        notes=args.notes,
        include_fills=args.include_fills,
    )
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    if not args.quiet:
        print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        path = os.path.join(args.out_dir, default_out_name(doc))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {path}", file=sys.stderr)
    if args.fills_out:
        write_fills_csv(args.fills_out, engine.fills)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
