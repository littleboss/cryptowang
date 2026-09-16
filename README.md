# cryptowang

OKX 策略工程工具箱（纸面 / dry-run）。**推代码 ≠ 实盘。**

本仓只版本化可证伪假设、费用后计算和打印-only 的 amend 草案。没有密钥、没有提现、没有会发单的 HTTP 客户端。Freqtrade 仅作方法参照，**不启用 `freqtrade trade` live**。

## 布局

选的是**仓库根目录**（不套一层 `okx/`），对应原 `07-code/` 骨架：

| 路径 | 内容 |
|------|------|
| `strategies/` | 策略与参数 schema、OKX Bot amend **打印**适配 |
| `tools/` | 纸面校验、观察器（无密钥、无下单） |
| `notes/` | 筛选结论、Freqtrade 对照、v2 旁路笔记 |
| `backtests/` | 预留：回测脚本与费用后报告（本 PR 未加） |

## 硬门禁

1. **无密钥**：不提交 API key、`.env`、提现权限。`.gitignore` 已挡常见密钥文件。
2. **无提现 / 无跨所转账自动化**。
3. **默认现货 / ≤1x**；`>2x` 标红另批。本仓 dry-run 固定 `lever=1`。
4. **默认 dry-run**：`will_send_http=false`。未获用户明确确认 + 风控放行前，不得实写 / live amend。
5. **演示 fixture 的 `algoId`（默认 `demo-grid-eth-usdt-001`）不得用于 live amend。**
6. 纸面结果 **≠ OKX Bot 净值 / 收益承诺**。回撤口径必须用 Bot `total_pnl_ratio`，不是 Freqtrade hyperopt 曲线。
7. 先可证伪假设 → 再写代码 → 费用后回测 / 纸面。优化产出：diff + 前后对比 + 失效条件。

## 当前状态

- 演示提案 v1（`slTriggerPx=2150`）已附条件放行，等用户确认后才允许 dry-run 之外的动作。
- 本 PR 只入库纸面工具；**push ≠ 实盘**。

## 如何运行

Python 3 标准库即可，无需 `pip install`。

### 1. 网格费用敏感度（纸面）

证伪「保留 N 格」：若往返手续费 `>=` 单格振幅，则该格数站不住。

```bash
python3 tools/grid_fee_sensitivity.py --help

# 默认：ETH 演示区间 2200–3200、30 格、maker+taker
python3 tools/grid_fee_sensitivity.py

# Freqtrade 风格 worst-case：2 * max(maker, taker)
python3 tools/grid_fee_sensitivity.py --worst-case

# 与 fixture ~1.28% mid-span 对齐的格数口径
python3 tools/grid_fee_sensitivity.py --intervals grid_num_minus_1
```

输出含 `disclaimer`：纸面敏感度 ≠ OKX Bot 净值。

### 2. SL 观察器（只建议暂停）

借鉴 Freqtrade `StoplossGuard` 的 lookback + trade_limit，**不 PairLock、不自动重启、不 amend**。

```bash
python3 tools/sl_guard_observe.py --help

python3 tools/sl_guard_observe.py \
  --trade-limit 3 \
  --lookback-minutes 10080 \
  --hits '2026-09-16T10:00:00+08:00|sl,2026-09-16T12:00:00+08:00|sl'
```

### 3. OKX 网格 amend dry-run（只打印）

打印拟调用的 `amend-order-algo` 字段。`method=PRINT_ONLY`，`will_send_http=false`。默认 `algoId=demo-grid-eth-usdt-001`，**禁止拿去 live amend**。

```bash
python3 strategies/okx_grid_dry_run.py --help

python3 strategies/okx_grid_dry_run.py
# 可选：把 JSON 落到文件（仍不发 HTTP）
python3 strategies/okx_grid_dry_run.py --out /tmp/grid-amend-dry-run.json
```

默认只改绝对价 `slTriggerPx=2150`（不是 Freqtrade 相对比率）。不加仓、不加杠杆。

## CI

GitHub Actions 工作流 [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 在 **pull_request** 以及 **push 到 `master`** 时跑纸面冒烟：

1. 三个脚本的 `--help` 能启动（Python 3.12、标准库，不 `pip install`）。
2. 各跑一遍默认参数：`okx_grid_dry_run.py` 必须含 `"will_send_http": false` 和 `method: PRINT_ONLY`。

**CI 绿 ≠ 实盘。** 工作流不注入 Secrets、不发 HTTP、不 amend、不提现。**推代码 ≠ live。** 不要在 Actions 里配置 API key / `.env`。

## 笔记

- [`notes/2026-09-16-code-screen-from-reading-pack.md`](notes/2026-09-16-code-screen-from-reading-pack.md) — 哪些可进代码、哪些明确不做
- [`notes/2026-09-16-ft-source-audit.md`](notes/2026-09-16-ft-source-audit.md) — Freqtrade 对照：借什么、不借什么
- [`notes/2026-09-16-v2-tools.md`](notes/2026-09-16-v2-tools.md) — v2 旁路与冒烟口径

## 远程仓与流程

- GitHub：`https://github.com/littleboss/cryptowang`
- 流程：本地草稿 → 本仓（风控执行）→ 运维部署（Atlas）
- **推代码 ≠ 实盘**；改参 / 部署触实盘须用户明确确认
