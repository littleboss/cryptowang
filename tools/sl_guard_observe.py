#!/usr/bin/env python3
"""Paper StoplossGuard-style observer — NEVER locks or amends a bot.

Borrow from freqtrade/plugins/protections/stoploss_guard.py:
  if count(SL hits in lookback) >= trade_limit → suggest pause (human), not auto-restart.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass
class SlHit:
    ts: str  # ISO or CST string
    reason: str  # sl | trailing | force  (paper labels)


def parse_hits(raw: str) -> list[SlHit]:
    """Format: ts|reason,ts|reason  e.g. 2026-09-16T10:00:00+08:00|sl"""
    hits: list[SlHit] = []
    if not raw.strip():
        return hits
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ts, _, reason = part.partition("|")
        hits.append(SlHit(ts=ts.strip(), reason=(reason or "sl").strip()))
    return hits


def evaluate(
    hits: list[SlHit],
    trade_limit: int,
    lookback_minutes: int,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(SHANGHAI)
    window_start = now - timedelta(minutes=lookback_minutes)
    in_window: list[dict] = []
    for h in hits:
        try:
            t = datetime.fromisoformat(h.ts.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
        if t >= window_start:
            in_window.append({"ts": h.ts, "reason": h.reason})

    triggered = len(in_window) >= trade_limit
    return {
        "mode": "paper_observe",
        "disclaimer": "只观察/建议暂停；不 PairLock、不自动重启、不 amend",
        "freqtrade_borrow": "StoplossGuard trade_limit + lookback",
        "lookback_minutes": lookback_minutes,
        "trade_limit": trade_limit,
        "hits_in_window": in_window,
        "count": len(in_window),
        "suggest_pause": triggered,
        "action_if_triggered": "人工确认后建议停止/暂停 Bot，出 v3；禁止自动加仓",
        "ts": now.strftime("%Y-%m-%d %H:%M:%S CST"),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Paper SL-guard observer")
    p.add_argument("--trade-limit", type=int, default=3)
    p.add_argument("--lookback-minutes", type=int, default=10080)  # 7d
    p.add_argument(
        "--hits",
        type=str,
        default="",
        help="comma list ts|reason",
    )
    args = p.parse_args()
    out = evaluate(parse_hits(args.hits), args.trade_limit, args.lookback_minutes)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
