#!/usr/bin/env python3
"""Strategy module: consume proposal v3 JSON, run baseline vs paper experiment arms
through tools/local_paper_grid.py on the *same* candle path, score the falsifiable
hypotheses H-A / H-B / H-C and emit a comparison JSON + markdown table.

Paper only. venue=local_paper. Output ≠ OKX Bot equity, ≠ a return promise, and
never marks an arm as "adopted" — adoption needs risk review + user confirmation.

Hard gates:
  * proposal must carry will_send_http=false; every arm must be spot lever=1;
  * an experiment arm may not raise investment or lever vs baseline (H-C veto);
  * no orders, no amend, no secrets; the only network I/O is the optional
    read-only public candles GET via tools/okx_readonly_client.py.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import local_paper_grid as lpg  # noqa: E402
import okx_readonly_client as okx  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")
VENUE = "local_paper"
DEFAULT_PROPOSAL = ROOT / "fixtures" / "proposals" / "2026-09-16-local-paper-eth-grid-001-v3.json"
LOCAL_PAPER_SCRIPT = ROOT / "tools" / "local_paper_grid.py"

DISCLAIMER = (
    "基线 vs 实验臂对照为本地 paper 撮合（venue=local_paper），≠ OKX Bot 净值，≠ 收益承诺；"
    "任何臂都不因本输出而「采纳」——须风控审后用户确认；零下单、零 amend、零密钥。"
)

# H-A falsification (proposal §2): 7 consecutive EODs with fee_after ≤ Day-1 and arb/day < 1.
HA_CONSECUTIVE_DAYS = 7
HA_MIN_ARB_PER_DAY = 1.0
# H-B falsification: B1 fee_after not better than baseline, or MDD up by > 2 pct points.
HB_MDD_TOLERANCE = 0.02
# Day-1 inventory-risk rule (proposal §4): buy/sell > 3 with worsening float.
INVENTORY_BUY_SELL_RATIO = 3.0

PNL_RATIO_INVALIDATION = lpg.PNL_RATIO_INVALIDATION
MS_PER_DAY = 86_400_000


# --------------------------------------------------------------------------- proposal


@dataclass(frozen=True)
class Arm:
    name: str
    role: str  # baseline | experiment
    params: lpg.GridParams
    requires: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "params": self.params.to_schema(),
            "requires": list(self.requires),
        }


@dataclass(frozen=True)
class Proposal:
    path: str
    version: str
    run_id: str
    venue: str
    status: str
    will_send_http: bool
    baseline: Arm
    experiments: tuple[Arm, ...]
    day1_reference: dict
    hypotheses: tuple[str, ...]
    invalidation: tuple[str, ...]
    proposed_hold: dict

    @property
    def arms(self) -> tuple[Arm, ...]:
        return (self.baseline, *self.experiments)

    def arm(self, name: str) -> Arm:
        for a in self.arms:
            if a.name == name:
                return a
        raise KeyError(f"unknown arm {name!r}; have {[a.name for a in self.arms]}")

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "version": self.version,
            "runId": self.run_id,
            "venue": self.venue,
            "status": self.status,
            "will_send_http": self.will_send_http,
            "arms": [a.to_dict() for a in self.arms],
            "hypotheses": list(self.hypotheses),
            "invalidation": list(self.invalidation),
            "proposed_hold": self.proposed_hold,
        }


def _params_from_block(block: dict, label: str) -> lpg.GridParams:
    try:
        p = lpg.GridParams(
            min_px=float(block["minPx"]),
            max_px=float(block["maxPx"]),
            grid_num=int(block["gridNum"]),
            investment=float(block["investment"]),
            sl_trigger_px=float(block["slTriggerPx"])
            if block.get("slTriggerPx") is not None
            else None,
            tp_trigger_px=float(block["tpTriggerPx"])
            if block.get("tpTriggerPx") is not None
            else None,
            lever=int(block.get("lever", 1)),
        )
    except KeyError as e:
        raise ValueError(f"proposal arm {label!r} missing field {e}") from None
    p.validate()  # raises for lever != 1, bad band, SL above minPx
    return p


def load_proposal(path: str | os.PathLike) -> Proposal:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("will_send_http", False) is not False:
        raise ValueError("refused: proposal must carry will_send_http=false")
    base_block = doc.get("baseline")
    if not isinstance(base_block, dict):
        raise ValueError("proposal needs a 'baseline' block")
    baseline = Arm("baseline", "baseline", _params_from_block(base_block, "baseline"))
    experiments: list[Arm] = []
    for key, block in doc.items():
        if not key.startswith("paper_experiment_") or not isinstance(block, dict):
            continue
        name = key[len("paper_experiment_") :] or key
        params = _params_from_block(block, key)
        if params.investment > baseline.params.investment:
            raise ValueError(f"refused (H-C): arm {name} raises investment vs baseline")
        if params.lever > baseline.params.lever:
            raise ValueError(f"refused (H-C): arm {name} raises lever vs baseline")
        experiments.append(Arm(name, "experiment", params, tuple(block.get("requires", []))))
    day1 = {k[len("day1_") :]: v for k, v in base_block.items() if k.startswith("day1_")}
    return Proposal(
        path=str(path),
        version=str(doc.get("version", "?")),
        run_id=str(doc.get("runId", lpg.DEFAULT_RUN_ID)),
        venue=str(doc.get("venue", VENUE)),
        status=str(doc.get("status", "?")),
        will_send_http=False,
        baseline=baseline,
        experiments=tuple(experiments),
        day1_reference=day1,
        hypotheses=tuple(doc.get("hypotheses", [])),
        invalidation=tuple(doc.get("invalidation", [])),
        proposed_hold=doc.get("proposed_hold", {}),
    )


# --------------------------------------------------------------------------- arm runs


@dataclass
class ArmResult:
    arm: Arm
    report: dict  # full sim-daily-schema doc from local_paper_grid
    untouched_grid_fraction: float

    @property
    def metrics(self) -> dict:
        return self.report["metrics"]

    @property
    def flags(self) -> dict:
        return self.report["invalidation_flags"]


def _untouched_fraction(fills: list[dict], grid_num: int) -> float:
    touched = {f["grid_idx"] for f in fills if f.get("grid_idx", -1) >= 0}
    return round(1.0 - len(touched) / grid_num, 6) if grid_num else 0.0


def run_arm_import(
    arm: Arm,
    candles: list[lpg.Candle],
    fees: lpg.FeeModel,
    *,
    run_id: str,
    inst_id: str,
    source_kind: str,
    data_source: dict,
    window_label: str,
    sl_slippage_bps: float = 0.0,
) -> ArmResult:
    engine = lpg.LocalPaperGrid(arm.params, fees, sl_slippage_bps=sl_slippage_bps)
    engine.run(candles)
    report = lpg.build_report(
        engine,
        run_id=f"{run_id}-{arm.name}",
        inst_id=inst_id,
        source_kind=source_kind,
        data_source=dict(data_source),
        window_label=window_label,
        notes=f"arm={arm.name} role={arm.role}; A/B paper compare; not a Bot change.",
    )
    fills = [{"grid_idx": f.grid_idx} for f in engine.fills]
    return ArmResult(arm, report, _untouched_fraction(fills, arm.params.grid_num))


def run_arm_subprocess(
    arm: Arm,
    candles: list[lpg.Candle],
    fees: lpg.FeeModel,
    *,
    run_id: str,
    inst_id: str,
    window_label: str,
    sl_slippage_bps: float = 0.0,
    python: str = sys.executable,
) -> ArmResult:
    """Same result via the CLI: dump candles to CSV, call local_paper_grid.py --source csv."""
    with tempfile.TemporaryDirectory() as d:
        csv_path = os.path.join(d, "candles.csv")
        out_path = os.path.join(d, "report.json")
        lpg.save_csv(csv_path, candles)
        argv = [
            python,
            str(LOCAL_PAPER_SCRIPT),
            "--source",
            "csv",
            "--candles-csv",
            csv_path,
            "--inst-id",
            inst_id,
            "--min-px",
            str(arm.params.min_px),
            "--max-px",
            str(arm.params.max_px),
            "--grid-num",
            str(arm.params.grid_num),
            "--investment",
            str(arm.params.investment),
            "--maker",
            str(fees.maker),
            "--taker",
            str(fees.taker),
            "--fee-mode",
            "worst_case" if fees.worst_case else "maker_taker",
            "--sl-slippage-bps",
            str(sl_slippage_bps),
            "--run-id",
            f"{run_id}-{arm.name}",
            "--window-label",
            window_label,
            "--include-fills",
            "1000000",
            "--quiet",
            "--out",
            out_path,
            "--notes",
            f"arm={arm.name} role={arm.role}; A/B paper compare; not a Bot change.",
        ]
        if arm.params.sl_trigger_px is None:
            argv.append("--no-sl")
        else:
            argv += ["--sl-trigger-px", str(arm.params.sl_trigger_px)]
        if arm.params.tp_trigger_px is not None:
            argv += ["--tp-trigger-px", str(arm.params.tp_trigger_px)]
        subprocess.run(argv, check=True, cwd=ROOT, capture_output=True, text=True)
        with open(out_path, encoding="utf-8") as f:
            report = json.load(f)
    fills = report.pop("fills_preview", [])
    return ArmResult(arm, report, _untouched_fraction(fills, arm.params.grid_num))


# --------------------------------------------------------------------------- scoring


def parse_cst_label(text: str | None) -> int | None:
    """Accept fmt_cst output ('YYYY-MM-DD HH:MM:SS CST') as well as plain CST / ISO strings."""
    if not text:
        return None
    return lpg.parse_cst(text.removesuffix(" CST"))


def window_days(report: dict) -> float:
    ds = report.get("data_source") or {}
    m = report["metrics"]
    n = int(m.get("candles") or ds.get("candles") or 0)
    if n <= 0:
        return 0.0
    first = parse_cst_label((report.get("window") or {}).get("from"))
    last = parse_cst_label((report.get("window") or {}).get("to"))
    if first is None or last is None or last <= first:
        return 0.0
    bar_ms = (last - first) / max(n - 1, 1)
    return (last - first + bar_ms) / MS_PER_DAY


def buy_sell_ratio(m: dict) -> float | None:
    sells = m.get("sell_count") or 0
    buys = m.get("buy_count") or 0
    if sells == 0:
        return float("inf") if buys > 0 else None
    return buys / sells


def inventory_risk(m: dict) -> bool:
    r = buy_sell_ratio(m)
    return r is not None and r > INVENTORY_BUY_SELL_RATIO and (m.get("float_profit") or 0.0) < 0.0


def invalidation_matrix(res: ArmResult, names: tuple[str, ...]) -> dict:
    m, f = res.metrics, res.flags
    computed = {
        "price_below_2200": bool(f.get("below_min_px") or f.get("below_min_px_ever")),
        "sl_hit_2150": bool(f.get("sl_triggered")),
        "okx_bot_total_pnl_ratio_lte_-0.08": bool(
            f.get("pnl_ratio_lte_minus_8pct") or f.get("pnl_ratio_lte_minus_8pct_ever")
        ),
        "fee_gte_per_grid": bool(f.get("fee_gte_per_grid")),
        "buy_sell_ratio_gt_3_with_worsening_float": inventory_risk(m),
    }
    # keep proposal order; unknown proposal names are reported as null (not silently dropped)
    out = {name: computed.get(name) for name in names}
    for k, v in computed.items():
        out.setdefault(k, v)
    out["any"] = any(bool(v) for k, v in out.items() if k != "any")
    return out


def arm_summary(res: ArmResult, names: tuple[str, ...]) -> dict:
    m = res.metrics
    return {
        "role": res.arm.role,
        "params": res.arm.params.to_schema(),
        "per_grid_pct": round(res.arm.params.per_grid_pct, 8),
        "fee_round_trip": m.get("fee_round_trip_paper"),
        "fee_after_pnl_est": m["fee_after_pnl_est"],
        "fee_after_pnl_ratio_est": m.get("fee_after_pnl_ratio_est"),
        "okx_bot_total_pnl": m["okx_bot_total_pnl"],
        "okx_bot_total_pnl_ratio": m["okx_bot_total_pnl_ratio"],
        "fees_paid_est": m["fees_paid_est"],
        "arbitrage_num": m["arbitrage_num"],
        "buy_count": m["buy_count"],
        "sell_count": m["sell_count"],
        "buy_sell_ratio": (lambda r: None if r is None or r == float("inf") else round(r, 4))(
            buy_sell_ratio(m)
        ),
        "max_drawdown_ratio": m["max_drawdown_ratio"],
        "fee_falsified": bool(m.get("fee_falsified")),
        "untouched_grid_fraction": res.untouched_grid_fraction,
        "last_px": m.get("last_px"),
        "stop_reason": m.get("stop_reason"),
        "candles": m.get("candles"),
        "window_days": round(window_days(res.report), 4),
        "invalidation": invalidation_matrix(res, names),
        "runId": res.report["runId"],
    }


def load_history(history_dir: str, run_id: str) -> list[dict]:
    """EOD metrics JSONs named YYYY-MM-DD-<runId>.json (sim-daily-schema), sorted by date."""
    if not history_dir:
        return []
    paths = sorted(glob.glob(os.path.join(history_dir, f"*-{run_id}.json")))
    out = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("metrics"), dict):
            doc["_path"] = p
            out.append(doc)
    return out


def score_h_a(base: ArmResult, day1: dict, history: list[dict]) -> dict:
    """H-A: wide band 2200–3200 / 30 grids turns fee-after positive under in-band chop.

    Falsified when `HA_CONSECUTIVE_DAYS` consecutive EODs (history + this run) all have
    fee_after_pnl_est ≤ Day-1 reference AND arbitrage/day < 1.
    """
    ref_fee_after = day1.get("fee_after_pnl_est")
    m = base.metrics
    days = window_days(base.report)
    arb_per_day = (m["arbitrage_num"] / days) if days > 0 else None
    series: list[dict] = []
    for h in history:
        hm = h["metrics"]
        hd = (window_days(h) if h.get("window") else 0.0) or 1.0  # EOD file ≈ one day
        series.append(
            {
                "source": h.get("_path"),
                "fee_after_pnl_est": hm.get("fee_after_pnl_est"),
                "arb_per_day": (hm.get("arbitrage_num", 0) / hd) if hd > 0 else None,
            }
        )
    series.append(
        {
            "source": "this_run",
            "fee_after_pnl_est": m["fee_after_pnl_est"],
            "arb_per_day": arb_per_day,
        }
    )

    def bad(day: dict) -> bool:
        fa, apd = day["fee_after_pnl_est"], day["arb_per_day"]
        if fa is None or apd is None or ref_fee_after is None:
            return False
        return fa <= ref_fee_after and apd < HA_MIN_ARB_PER_DAY

    streak = 0
    for day in reversed(series):
        if bad(day):
            streak += 1
        else:
            break
    if m["fee_after_pnl_est"] > 0:
        status = "supported_on_window"
    elif streak >= HA_CONSECUTIVE_DAYS:
        status = "falsified"
    else:
        status = "not_yet_falsified"
    return {
        "id": "H-A",
        "hypothesis": "维持宽区间 2200–3200 + 30 格，带内震荡下费用后权益会转正",
        "falsification_rule": (
            f"连续 {HA_CONSECUTIVE_DAYS} 个 EOD：fee_after_pnl_est ≤ Day-1 且 套利/日 < "
            f"{HA_MIN_ARB_PER_DAY:g}"
        ),
        "status": status,
        "evidence": {
            "arm": base.arm.name,
            "fee_after_pnl_est": m["fee_after_pnl_est"],
            "day1_fee_after_pnl_est": ref_fee_after,
            "arbitrage_num": m["arbitrage_num"],
            "arb_per_day": None if arb_per_day is None else round(arb_per_day, 4),
            "window_days": round(days, 4),
            "consecutive_bad_eods": streak,
            "eods_considered": len(series),
            "days_until_falsifiable": max(HA_CONSECUTIVE_DAYS - streak, 0),
        },
        "note": "单窗样本；7 日滚动优先。supported_on_window ≠ 采纳。",
    }


def score_h_b(base: ArmResult, exp: ArmResult) -> dict:
    """H-B: idle upper band drags capital efficiency; narrowing maxPx raises arbitrage per unit
    capital. Falsified when the narrowed arm's fee_after is not better than baseline, or its MDD
    rises by more than HB_MDD_TOLERANCE (2 pct points). Paper only."""
    bm, em = base.metrics, exp.metrics
    d_fee_after = em["fee_after_pnl_est"] - bm["fee_after_pnl_est"]
    d_mdd = em["max_drawdown_ratio"] - bm["max_drawdown_ratio"]
    d_arb = em["arbitrage_num"] - bm["arbitrage_num"]
    fee_not_better = d_fee_after <= 0.0
    mdd_worse = d_mdd > HB_MDD_TOLERANCE
    inv = base.arm.params.investment
    status = "falsified_on_window" if (fee_not_better or mdd_worse) else "supported_on_window"
    reasons = []
    if fee_not_better:
        reasons.append("fee_after_not_better_than_baseline")
    if mdd_worse:
        reasons.append(f"mdd_up_gt_{HB_MDD_TOLERANCE:g}")
    return {
        "id": "H-B",
        "hypothesis": (
            "价长期偏下半区时上沿闲置拖累资金效率；收窄 maxPx 可提高单位资金套利（仅 paper）"
        ),
        "falsification_rule": (
            "同窗同费：收窄后 fee_after 不优于基线，或 max_drawdown_ratio 升幅 > "
            f"{HB_MDD_TOLERANCE:g}"
        ),
        "status": status,
        "falsified_by": reasons,
        "evidence": {
            "baseline": base.arm.name,
            "experiment": exp.arm.name,
            "delta_fee_after_pnl_est": round(d_fee_after, 6),
            "delta_okx_bot_total_pnl_ratio": round(
                em["okx_bot_total_pnl_ratio"] - bm["okx_bot_total_pnl_ratio"], 8
            ),
            "delta_arbitrage_num": d_arb,
            "delta_max_drawdown_ratio": round(d_mdd, 8),
            "arb_per_1000_quote": {
                base.arm.name: round(bm["arbitrage_num"] / inv * 1000.0, 4),
                exp.arm.name: round(em["arbitrage_num"] / exp.arm.params.investment * 1000.0, 4),
            },
            "untouched_grid_fraction": {
                base.arm.name: base.untouched_grid_fraction,
                exp.arm.name: exp.untouched_grid_fraction,
            },
            "per_grid_pct": {
                base.arm.name: round(base.arm.params.per_grid_pct, 8),
                exp.arm.name: round(exp.arm.params.per_grid_pct, 8),
            },
            "above_max_px_candles": {
                base.arm.name: bm.get("above_max_px_candles"),
                exp.arm.name: em.get("above_max_px_candles"),
            },
        },
        "note": (
            "同一 K 线路径、同一费率；无滑点/深度 → 乐观于真实 Bot；未样本外验证前不得当改参依据。"
        ),
    }


def score_h_c(prop: Proposal, results: dict[str, ArmResult]) -> dict:
    """H-C: with long inventory (buys ≫ sells) do NOT add capital / densify. Any add-position or
    leverage proposal is vetoed at load time; here we report the policy check per arm plus the
    Day-1 inventory-risk flag (buy/sell > 3 with negative float)."""
    base = prop.baseline.params
    arms = {}
    vetoes = []
    for name, res in results.items():
        p = res.arm.params
        adds_capital = p.investment > base.investment
        adds_lever = p.lever > 1 or p.lever > base.lever
        if adds_capital or adds_lever:
            vetoes.append(name)
        m = res.metrics
        r = buy_sell_ratio(m)
        arms[name] = {
            "investment": p.investment,
            "lever": p.lever,
            "adds_capital": adds_capital,
            "adds_lever": adds_lever,
            "buy_sell_ratio": None if r is None or r == float("inf") else round(r, 4),
            "float_profit": m.get("float_profit"),
            "inventory_risk": inventory_risk(m),
            "max_drawdown_ratio": m["max_drawdown_ratio"],
        }
    any_inv_risk = any(a["inventory_risk"] for a in arms.values())
    return {
        "id": "H-C",
        "hypothesis": "库存偏多（买≫卖）时不加仓、不加密格优于摊平",
        "falsification_rule": (
            "任何加仓/加杠杆提案直接风控否决；paper 若强制加仓则 max_drawdown_ratio 恶化"
        ),
        "status": "vetoed_arms_present" if vetoes else "policy_enforced",
        "vetoed_arms": vetoes,
        "inventory_risk_arms": [n for n, a in arms.items() if a["inventory_risk"]],
        "add_position_proposals_allowed": False,
        "forced_add_stress_test": "not_run_by_design (加仓臂被风控否决，本模块不构造加仓臂)",
        "evidence": {"arms": arms, "inventory_risk_any": any_inv_risk},
        "note": "inventory_risk=true → 标记「库存风险」，禁止加仓提案；不摊平、不加密格。",
    }


# --------------------------------------------------------------------------- report


def build_comparison(
    prop: Proposal,
    results: dict[str, ArmResult],
    *,
    candle_path: dict,
    fee_model: dict,
    engine: str,
    history: list[dict],
) -> dict:
    base = results[prop.baseline.name]
    arms = {name: arm_summary(res, prop.invalidation) for name, res in results.items()}
    deltas = {}
    for name, res in results.items():
        if name == prop.baseline.name:
            continue
        bm, em = base.metrics, res.metrics
        deltas[name] = {
            "vs": prop.baseline.name,
            "fee_after_pnl_est": round(em["fee_after_pnl_est"] - bm["fee_after_pnl_est"], 6),
            "okx_bot_total_pnl_ratio": round(
                em["okx_bot_total_pnl_ratio"] - bm["okx_bot_total_pnl_ratio"], 8
            ),
            "arbitrage_num": em["arbitrage_num"] - bm["arbitrage_num"],
            "max_drawdown_ratio": round(em["max_drawdown_ratio"] - bm["max_drawdown_ratio"], 8),
            "fees_paid_est": round(em["fees_paid_est"] - bm["fees_paid_est"], 6),
        }
    hyps: dict[str, dict] = {}
    if "H-A" in prop.hypotheses or not prop.hypotheses:
        hyps["H-A"] = score_h_a(base, prop.day1_reference, history)
    if "H-B" in prop.hypotheses or not prop.hypotheses:
        hyps["H-B"] = {
            name: score_h_b(base, res)
            for name, res in results.items()
            if name != prop.baseline.name
        }
    if "H-C" in prop.hypotheses or not prop.hypotheses:
        hyps["H-C"] = score_h_c(prop, results)
    return {
        "generated_at": datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S CST"),
        "mode": "模拟",
        "venue": VENUE,
        "kind": "grid_ab_compare",
        "engine": engine,
        "proposal": prop.to_dict(),
        "day1_reference": prop.day1_reference,
        "candle_path": candle_path,
        "fee_model": fee_model,
        "arms": arms,
        "deltas": deltas,
        "hypotheses": hyps,
        "adoption": {
            "adopted": False,
            "bot_changed": False,
            "requires": sorted(
                {r for a in prop.experiments for r in a.requires}
                | {"risk_review", "user_confirm_before_adopt"}
            ),
            "note": "paper 对照结果不构成改参依据；写入 05-reviews 时不得暗示「已改 Bot」",
        },
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
    }


def _fmt(v, pct: bool = False, nd: int = 4) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if pct:
        return f"{v * 100:.2f}%"
    return f"{v:.{nd}f}"


def render_markdown(doc: dict) -> str:
    prop = doc["proposal"]
    lines = [
        f"# Grid A/B paper compare · {prop['runId']} · proposal {prop['version']}",
        "",
        f"- venue: `{doc['venue']}` · engine: `{doc['engine']}` · generated: {doc['generated_at']}",
        f"- candle path: `{doc['candle_path'].get('kind')}` · candles: "
        f"{doc['candle_path'].get('candles')} · {doc['candle_path'].get('first_ts')} → "
        f"{doc['candle_path'].get('last_ts')}",
        f"- fee model: maker {doc['fee_model']['maker']} / taker {doc['fee_model']['taker']} · "
        f"{doc['fee_model']['mode']} · round-trip {doc['fee_model']['round_trip_paper']}",
        "- will_send_http: `false` · adopted: `false` · bot_changed: `false`",
        "",
        "## Arms",
        "",
        "| arm | role | band / grids | per_grid % | fee_after | pnl_ratio | arb | MDD | fees | "
        "buy/sell | fee_falsified | untouched grids | invalidation |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, a in doc["arms"].items():
        p = a["params"]
        inv = a["invalidation"]
        hits = [k for k, v in inv.items() if k != "any" and v]
        lines.append(
            f"| {name} | {a['role']} | {p['minPx']:g}–{p['maxPx']:g} / {p['gridNum']} | "
            f"{_fmt(a['per_grid_pct'], pct=True)} | {_fmt(a['fee_after_pnl_est'])} | "
            f"{_fmt(a['okx_bot_total_pnl_ratio'], pct=True)} | {a['arbitrage_num']} | "
            f"{_fmt(a['max_drawdown_ratio'], pct=True)} | {_fmt(a['fees_paid_est'])} | "
            f"{a['buy_count']}/{a['sell_count']} | {_fmt(a['fee_falsified'])} | "
            f"{_fmt(a['untouched_grid_fraction'], pct=True)} | "
            f"{', '.join(hits) if hits else 'none'} |"
        )
    if doc["deltas"]:
        lines += [
            "",
            "## Deltas vs baseline",
            "",
            "| arm | Δ fee_after | Δ pnl_ratio | Δ arb | Δ MDD | Δ fees |",
            "|---|---|---|---|---|---|",
        ]
        for name, d in doc["deltas"].items():
            lines.append(
                f"| {name} | {_fmt(d['fee_after_pnl_est'])} | "
                f"{_fmt(d['okx_bot_total_pnl_ratio'], pct=True)} | {d['arbitrage_num']} | "
                f"{_fmt(d['max_drawdown_ratio'], pct=True)} | {_fmt(d['fees_paid_est'])} |"
            )
    lines += ["", "## Hypotheses", "", "| id | status | key evidence |", "|---|---|---|"]
    h = doc["hypotheses"]
    if "H-A" in h:
        e = h["H-A"]["evidence"]
        lines.append(
            f"| H-A | `{h['H-A']['status']}` | fee_after {_fmt(e['fee_after_pnl_est'])} vs Day-1 "
            f"{_fmt(e['day1_fee_after_pnl_est'])}; arb/day {_fmt(e['arb_per_day'])}; "
            f"bad EOD streak {e['consecutive_bad_eods']}/{HA_CONSECUTIVE_DAYS} |"
        )
    if "H-B" in h:
        for name, hb in h["H-B"].items():
            e = hb["evidence"]
            lines.append(
                f"| H-B ({name}) | `{hb['status']}` | Δ fee_after "
                f"{_fmt(e['delta_fee_after_pnl_est'])}; Δ MDD "
                f"{_fmt(e['delta_max_drawdown_ratio'], pct=True)}; Δ arb {e['delta_arbitrage_num']}"
                f"{'; ' + ', '.join(hb['falsified_by']) if hb['falsified_by'] else ''} |"
            )
    if "H-C" in h:
        hc = h["H-C"]
        lines.append(
            f"| H-C | `{hc['status']}` | vetoed: {hc['vetoed_arms'] or 'none'}; inventory_risk: "
            f"{hc['inventory_risk_arms'] or 'none'}; add-position allowed: no |"
        )
    lines += ["", f"> {doc['disclaimer']}", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Baseline vs paper-experiment grid compare on one candle path "
        "(venue=local_paper). Scores H-A/H-B/H-C. Paper ≠ OKX Bot equity; nothing is adopted; "
        "no orders, no amend, no secrets.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--proposal", default=str(DEFAULT_PROPOSAL), help="proposal v3 JSON")
    p.add_argument("--arms", default="", help="comma list of arms to run (default: all)")
    p.add_argument("--inst-id", default="ETH-USDT")
    p.add_argument(
        "--engine",
        choices=["import", "subprocess"],
        default="import",
        help="import: call the engine in-process; subprocess: run local_paper_grid.py CLI",
    )

    src = p.add_argument_group("candle path (identical for every arm)")
    src.add_argument("--source", choices=["synthetic", "csv", "okx-public"], default="synthetic")
    src.add_argument("--candles-csv", default="", help="CSV path for --source csv")
    src.add_argument("--bar", default="5m", help="OKX bar for --source okx-public")
    src.add_argument("--limit", type=int, default=288, help="per-request candles (max 300)")
    src.add_argument("--pages", type=int, default=1)
    src.add_argument("--save-candles-csv", default="", help="dump ingested candles for replay")
    src.add_argument("--synthetic-start", type=float, default=2450.0, help="inside both bands")
    src.add_argument("--synthetic-amp", type=float, default=200.0)
    src.add_argument("--synthetic-period", type=int, default=240)
    src.add_argument("--synthetic-steps", type=int, default=1440)
    src.add_argument("--synthetic-noise", type=float, default=3.0)
    src.add_argument("--synthetic-drift", type=float, default=0.0)
    src.add_argument("--synthetic-start-ts", default="", help="CST 'YYYY-MM-DD HH:MM'")
    src.add_argument("--seed", type=int, default=20260916)
    src.add_argument("--window-from", default="", help="CST 'YYYY-MM-DD HH:MM'")
    src.add_argument("--window-to", default="", help="CST 'YYYY-MM-DD HH:MM'")
    src.add_argument("--window-label", default="", help="eod|intraday|synthetic (auto)")

    f = p.add_argument_group("fees (same for every arm)")
    f.add_argument("--maker", type=float, default=0.0008)
    f.add_argument("--taker", type=float, default=0.0010)
    f.add_argument("--fee-mode", choices=["worst_case", "maker_taker"], default="worst_case")
    f.add_argument("--sl-slippage-bps", type=float, default=0.0)

    o = p.add_argument_group("scoring / output")
    o.add_argument(
        "--history-dir",
        default="",
        help="dir of EOD metrics JSON (YYYY-MM-DD-<runId>.json) for H-A 7-day rule",
    )
    o.add_argument("--out", default="", help="write comparison JSON here")
    o.add_argument("--md-out", default="", help="write markdown table here")
    o.add_argument("--arm-reports-dir", default="", help="write full per-arm schema JSON here")
    o.add_argument("--quiet", action="store_true", help="do not print JSON to stdout")
    o.add_argument("--print-md", action="store_true", help="print the markdown table to stdout")
    return p


def load_candles(args) -> tuple[list[lpg.Candle], dict, str]:
    if args.source == "synthetic":
        start_ts = lpg.parse_cst(args.synthetic_start_ts) if args.synthetic_start_ts else None
        candles = lpg.gen_synthetic(
            args.synthetic_start,
            args.synthetic_amp,
            args.synthetic_period,
            args.synthetic_steps,
            args.synthetic_noise,
            args.seed,
            drift_per_step=args.synthetic_drift,
            start_ts_ms=start_ts,
        )
        ds = {
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
            raise SystemExit("--candles-csv is required for --source csv")
        candles = lpg.load_csv(args.candles_csv)
        ds = {"kind": "csv", "http_fetch": False, "path": args.candles_csv}
        label = args.window_label or "eod"
    else:
        client = okx.OkxPublicClient()
        rows = client.get_candles(args.inst_id, bar=args.bar, limit=args.limit, pages=args.pages)
        candles = [lpg.Candle(r.ts_ms, r.open, r.high, r.low, r.close) for r in rows]
        ds = client.data_source_meta(args.inst_id, args.bar, args.limit, args.pages)
        label = args.window_label or "eod"
    from_ms = lpg.parse_cst(args.window_from) if args.window_from else None
    to_ms = lpg.parse_cst(args.window_to) if args.window_to else None
    candles = lpg.clip_window(candles, from_ms, to_ms)
    ds["candles"] = len(candles)
    if candles:
        ds["first_ts"] = lpg.fmt_cst(candles[0].ts_ms)
        ds["last_ts"] = lpg.fmt_cst(candles[-1].ts_ms)
    if args.save_candles_csv:
        lpg.save_csv(args.save_candles_csv, candles)
        ds["saved_csv"] = args.save_candles_csv
    return candles, ds, label


def compare(
    prop: Proposal,
    candles: list[lpg.Candle],
    fees: lpg.FeeModel,
    *,
    arm_names: list[str] | None = None,
    engine: str = "import",
    inst_id: str = "ETH-USDT",
    source_kind: str = "synthetic",
    data_source: dict | None = None,
    window_label: str = "synthetic",
    sl_slippage_bps: float = 0.0,
    history: list[dict] | None = None,
) -> tuple[dict, dict[str, ArmResult]]:
    names = arm_names or [a.name for a in prop.arms]
    if prop.baseline.name not in names:
        names = [prop.baseline.name, *names]
    ds = data_source or {"kind": source_kind, "http_fetch": False, "candles": len(candles)}
    results: dict[str, ArmResult] = {}
    for name in names:
        arm = prop.arm(name)
        if engine == "subprocess":
            results[name] = run_arm_subprocess(
                arm,
                candles,
                fees,
                run_id=prop.run_id,
                inst_id=inst_id,
                window_label=window_label,
                sl_slippage_bps=sl_slippage_bps,
            )
            results[name].report["data_source"] = {
                **ds,
                **results[name].report.get("data_source", {}),
            }
        else:
            results[name] = run_arm_import(
                arm,
                candles,
                fees,
                run_id=prop.run_id,
                inst_id=inst_id,
                source_kind=source_kind,
                data_source=ds,
                window_label=window_label,
                sl_slippage_bps=sl_slippage_bps,
            )
    doc = build_comparison(
        prop,
        results,
        candle_path=ds,
        fee_model=fees.to_dict(),
        engine=engine,
        history=history or [],
    )
    return doc, results


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prop = load_proposal(args.proposal)
    fees = lpg.FeeModel(
        maker=args.maker, taker=args.taker, worst_case=args.fee_mode == "worst_case"
    )
    candles, ds, label = load_candles(args)
    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()] or None
    history = load_history(args.history_dir, prop.run_id)
    doc, results = compare(
        prop,
        candles,
        fees,
        arm_names=arm_names,
        engine=args.engine,
        inst_id=args.inst_id,
        source_kind=args.source,
        data_source=ds,
        window_label=label,
        sl_slippage_bps=args.sl_slippage_bps,
        history=history,
    )
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    md = render_markdown(doc)
    if not args.quiet:
        print(text)
    if args.print_md:
        print(md)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as fh:
            fh.write(md)
    if args.arm_reports_dir:
        os.makedirs(args.arm_reports_dir, exist_ok=True)
        for name, res in results.items():
            path = os.path.join(args.arm_reports_dir, lpg.default_out_name(res.report))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(res.report, ensure_ascii=False, indent=2) + "\n")
            print(f"wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
