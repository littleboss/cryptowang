# Freqtrade 源码对照审 `07-code/` · 2026-09-16

对照：`01-inbox/2026-09-16-freqtrade-source-notes.md` + 本地 `freqtrade-src` tag 2026.8

## 判定

| 项 | 此前 | 现况 |
|----|------|------|
| 费用后证伪 H1 | 有，显式 round-trip | **补** `--worst-case` → `2*max(maker,taker)`（对齐 `set_fee` worst-case） |
| dry-run 只打印 | 有 | **补** `confirm_checklist`（映射 `confirm_trade_*`，人工非模型） |
| SL 绝对价 | 有 | 维持；禁止相对比率 |
| StoplossGuard | 缺 | **新** `tools/sl_guard_observe.py`（只建议暂停，不 lock/重启） |
| 回撤口径 | 提案写 -8% | **补** dry-run 内 `drawdown_policy.basis=okx_bot_total_pnl_ratio` |
| 订单簿滑点 dry | 无 | **有意不做**（避免假装=FT dry exchange） |

## 仍不借

- `freqtrade trade` live / 自动 PairLock 重启
- 信号策略当网格
- hyperopt 直改官方 Bot

## 门禁

暂不推仓；v2 dry-run 仍等用户确认。源码深挖后 **维持 A**。
