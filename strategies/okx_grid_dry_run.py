#!/usr/bin/env python3
"""OKX spot-grid amend dry-run printer. Never sends HTTP. No secrets.

FT A-borrow: confirm_trade_* → human checklist (not model auto-approve).
Default action: only slTriggerPx; absolute price (not FT relative ratio).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
DRY_RUN_ENDPOINT = "POST /api/v5/tradingBot/grid/amend-order-algo  # dry-run label only"

CONFIRM_CHECKLIST = [
    "用户聊天已明确确认本改参（非模型 confirm）",
    "04-risk 已放行且版本匹配",
    "algoId 非 demo / 或演示模式已标明",
    "slTriggerPx 为绝对价，非 FT 相对比率",
    "不加仓、不加杠杆、不自动摊平",
]


def build_dry_run(
    algo_id: str,
    inst_id: str,
    sl_trigger_px: float | None,
    current: dict,
    drawdown_basis: str,
) -> dict:
    suggested = {
        "algoId": algo_id,
        "instId": inst_id,
        "slTriggerPx": str(sl_trigger_px) if sl_trigger_px is not None else None,
    }
    unchanged = {
        "investment": current.get("investment"),
        "lever": current.get("lever", "1"),
        "minPx": current.get("minPx"),
        "maxPx": current.get("maxPx"),
        "gridNum": current.get("gridNum"),
        "tpTriggerPx": current.get("tpTriggerPx"),
    }
    return {
        "mode": "dry-run",
        "disclaimer": "零实盘：仅打印拟调用接口与参数；需用户确认后才允许实写",
        "ts": datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S CST"),
        "endpoint": DRY_RUN_ENDPOINT,
        "method": "PRINT_ONLY",
        "will_send_http": False,
        "freqtrade_borrow": {
            "confirm_trade_hooks": "映射为 confirm_checklist（人工）",
            "stoploss_ratio": "禁止；OKX 用绝对 slTriggerPx",
            "dry_vs_backtest": "本工具连模拟成交都没有，更保守",
        },
        "confirm_checklist": CONFIRM_CHECKLIST,
        "drawdown_policy": {
            "basis": drawdown_basis,
            "threshold": -0.08,
            "note": "复盘必须写清：用 OKX Bot total_pnl_ratio（净值口径），不是 FT hyperopt ratio 曲线",
        },
        "current": current,
        "payload_delta": {k: v for k, v in suggested.items() if v is not None},
        "explicitly_unchanged": unchanged,
        "risk_notes": [
            "不加仓、不加杠杆",
            "触及失效条件只建议暂停，不自动摊平",
            "演示 algoId 不得用于 live amend",
            "频繁触 SL → 用 tools/sl_guard_observe.py 计数后人工停，不自动重启",
        ],
    }


def main() -> None:
    p = argparse.ArgumentParser(description="OKX grid amend dry-run (print only)")
    p.add_argument("--algo-id", default="demo-grid-eth-usdt-001")
    p.add_argument("--inst-id", default="ETH-USDT")
    p.add_argument("--sl-trigger-px", type=float, default=2150.0)
    p.add_argument("--investment", type=float, default=1000.0)
    p.add_argument("--min-px", type=float, default=2200.0)
    p.add_argument("--max-px", type=float, default=3200.0)
    p.add_argument("--grid-num", type=int, default=30)
    p.add_argument(
        "--drawdown-basis",
        default="okx_bot_total_pnl_ratio",
        help="must stay Bot equity based",
    )
    p.add_argument("--out", type=str, default="")
    args = p.parse_args()

    current = {
        "algoId": args.algo_id,
        "instId": args.inst_id,
        "investment": args.investment,
        "lever": "1",
        "minPx": args.min_px,
        "maxPx": args.max_px,
        "gridNum": args.grid_num,
        "slTriggerPx": None,
        "tpTriggerPx": None,
    }
    doc = build_dry_run(
        args.algo_id, args.inst_id, args.sl_trigger_px, current, args.drawdown_basis
    )
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
