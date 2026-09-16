#!/usr/bin/env python3
"""Paper grid fee sensitivity — NOT live PnL / NOT OKX Bot equity.

Freqtrade A-borrow: backtesting set_fee uses worst-case (max of tiers).
We keep round-trip fees (buy+sell), optional --worst-case maker/taker → 2*max.
Falsifies "keep N grids" if round_trip_fee >= per_grid_pct.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass


@dataclass
class FeeRow:
    round_trip_fee_pct: float
    net_grid_pct: float
    fee_share_of_grid: float
    falsified: bool
    fee_mode: str


def per_grid_pct(
    min_px: float,
    max_px: float,
    grid_num: int,
    mode: str = "arithmetic",
    intervals: str = "grid_num",
) -> float:
    if grid_num <= 0 or min_px <= 0 or max_px <= min_px:
        raise ValueError("need grid_num>0 and max_px>min_px>0")
    n = grid_num if intervals == "grid_num" else max(grid_num - 1, 1)
    if mode == "arithmetic":
        step = (max_px - min_px) / n
        mid = (min_px + max_px) / 2.0
        return step / mid
    if mode == "geometric":
        return (max_px / min_px) ** (1.0 / n) - 1.0
    raise ValueError("mode must be arithmetic|geometric")


def round_trip_from_sides(maker: float, taker: float, worst_case: bool) -> float:
    """FT dry market ≈ taker; backtest often worst-case single fee.
    Grid round-trip: two legs. worst_case → 2 * max(maker, taker).
    """
    if worst_case:
        return 2.0 * max(maker, taker)
    return maker + taker


def sensitivity(
    min_px: float,
    max_px: float,
    grid_num: int,
    fees: list[float],
    mode: str = "arithmetic",
    intervals: str = "grid_num",
    fee_mode: str = "explicit_round_trip",
) -> dict:
    pg = per_grid_pct(min_px, max_px, grid_num, mode=mode, intervals=intervals)
    rows = []
    for f in fees:
        net = pg - f
        share = (f / pg) if pg > 0 else float("inf")
        rows.append(
            FeeRow(
                round_trip_fee_pct=f,
                net_grid_pct=net,
                fee_share_of_grid=share,
                falsified=f >= pg,
                fee_mode=fee_mode,
            )
        )
    return {
        "mode": "paper",
        "disclaimer": "纸面敏感度 ≠ OKX Bot 净值；禁止当收益承诺。借鉴 FT worst-case fee 思想。",
        "freqtrade_borrow": {
            "set_fee_worst_case": True,
            "dry_market_as_taker": "可选 --taker 单边×2 近似",
            "not_implemented": "订单簿滑点插值（有意不做，避免假装=实盘）",
        },
        "grid": {
            "min_px": min_px,
            "max_px": max_px,
            "grid_num": grid_num,
            "intervals": intervals,
            "spacing": mode,
            "per_grid_pct": pg,
        },
        "rows": [asdict(r) for r in rows],
        "any_falsified": any(r.falsified for r in rows),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Grid fee sensitivity (paper only)")
    p.add_argument("--min-px", type=float, default=2200.0)
    p.add_argument("--max-px", type=float, default=3200.0)
    p.add_argument("--grid-num", type=int, default=30)
    p.add_argument("--mode", choices=["arithmetic", "geometric"], default="arithmetic")
    p.add_argument(
        "--intervals",
        choices=["grid_num", "grid_num_minus_1"],
        default="grid_num",
        help="grid_num_minus_1 ≈ demo metrics ~1.28% mid-span",
    )
    p.add_argument(
        "--fees",
        type=str,
        default="",
        help="comma round-trip ratios; empty → build from maker/taker",
    )
    p.add_argument("--maker", type=float, default=0.0008)
    p.add_argument("--taker", type=float, default=0.0010)
    p.add_argument(
        "--worst-case",
        action="store_true",
        help="FT-style: round_trip = 2 * max(maker, taker)",
    )
    args = p.parse_args()

    if args.fees.strip():
        fees = [float(x.strip()) for x in args.fees.split(",") if x.strip()]
        fee_mode = "explicit_round_trip"
    else:
        rt = round_trip_from_sides(args.maker, args.taker, args.worst_case)
        fees = [rt]
        fee_mode = "worst_case_2xmax" if args.worst_case else "maker_plus_taker"

    out = sensitivity(
        args.min_px,
        args.max_px,
        args.grid_num,
        fees,
        mode=args.mode,
        intervals=args.intervals,
        fee_mode=fee_mode,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
