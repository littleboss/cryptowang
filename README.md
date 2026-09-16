# cryptowang

OKX 策略工程工具箱（纸面 / dry-run）。**推代码 ≠ 实盘。**

本仓只版本化可证伪假设、费用后计算和打印-only 的 amend 草案。没有密钥、没有提现、没有会发单的 HTTP 客户端。Freqtrade 仅作方法参照，**不启用 `freqtrade trade` live**。

## 布局

选的是**仓库根目录**（不套一层 `okx/`），对应原 `07-code/` 骨架：

| 路径 | 内容 |
|------|------|
| `pyproject.toml` / `uv.lock` | uv 工程元数据与锁文件（仅打包，无交易依赖） |
| `strategies/` | 策略与参数 schema、OKX Bot amend **打印**适配 |
| `tools/` | 纸面校验、观察器、**本地 paper 网格成交模拟器**（无密钥、无下单） |
| `tests/` | 标准库 `unittest` 冒烟 / 单测（合成路径必须出成交） |
| `notes/` | 筛选结论、Freqtrade 对照、v2 旁路笔记 |
| `backtests/` | 预留：回测脚本与费用后报告（本 PR 未加） |

## 硬门禁

1. **无密钥**：不提交 API key、`.env`、提现权限。`.gitignore` 已挡常见密钥文件。
2. **无提现 / 无跨所转账自动化**。
3. **默认现货 / ≤1x**；`>2x` 标红另批。本仓 dry-run 与 local_paper 固定 `lever=1`。
4. **默认 dry-run**：`will_send_http=false`。未获用户明确确认 + 风控放行前，不得实写 / live amend。唯一允许的网络调用是 `local_paper_grid.py --source okx-public` 对 **公共行情** K 线的只读 GET（无签名、无私有 Trade API）。
5. **演示 fixture 的 `algoId`（默认 `demo-grid-eth-usdt-001`）不得用于 live amend。**
6. 纸面结果 **≠ OKX Bot 净值 / 收益承诺**。回撤口径必须用 Bot `total_pnl_ratio`，不是 Freqtrade hyperopt 曲线。
7. 先可证伪假设 → 再写代码 → 费用后回测 / 纸面。优化产出：diff + 前后对比 + 失效条件。

## 当前状态

- 演示提案 v1（`slTriggerPx=2150`）已附条件放行，等用户确认后才允许 dry-run 之外的动作。
- 模拟 venue 当前为 **`local_paper`**（OKX demo key 尚未到位，不接 `okx_demo`）。Day-0 metrics 为 bootstrap（0 新成交）；Day-1 起用 `tools/local_paper_grid.py` 产出。
- 本仓只入库纸面工具；**push ≠ 实盘**。

## 包装说明（仅工程，不是实盘）

本仓用 [`uv`](https://docs.astral.sh/uv/) + `pyproject.toml` 管理 Python 环境。**这只是打包 / 依赖锁定，不是开通实盘。**

- 运行时依赖为空：脚本仍是标准库，**没有**交易 / HTTP 下单客户端。
- `uv sync` / `uv run` **≠** 下单、**≠** 注入密钥、**≠** 启用 freqtrade live。
- 脚本保持在 `tools/` 与 `strategies/`，用 `uv run python …` 直接跑；没有把本仓装成可发单包。

## 如何运行

先装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，再同步环境（会按 `uv.lock` 建 `.venv`；运行时无需第三方包）：

```bash
uv sync
```

开发工具（可选，`dependency-groups.dev`，目前只有 ruff）：

```bash
uv sync --group dev
# 或默认也会装上 dev 组：uv sync
```

仍可用系统 `python3` 直接跑（标准库即可）。推荐用 `uv run`，与 CI 一致。

### 1. 网格费用敏感度（纸面）

证伪「保留 N 格」：若往返手续费 `>=` 单格振幅，则该格数站不住。

```bash
uv run python tools/grid_fee_sensitivity.py --help

# 默认：ETH 演示区间 2200–3200、30 格、maker+taker
uv run python tools/grid_fee_sensitivity.py

# Freqtrade 风格 worst-case：2 * max(maker, taker)
uv run python tools/grid_fee_sensitivity.py --worst-case

# 与 fixture ~1.28% mid-span 对齐的格数口径
uv run python tools/grid_fee_sensitivity.py --intervals grid_num_minus_1
```

输出含 `disclaimer`：纸面敏感度 ≠ OKX Bot 净值。

### 2. SL 观察器（只建议暂停）

借鉴 Freqtrade `StoplossGuard` 的 lookback + trade_limit，**不 PairLock、不自动重启、不 amend**。

```bash
uv run python tools/sl_guard_observe.py --help

uv run python tools/sl_guard_observe.py \
  --trade-limit 3 \
  --lookback-minutes 10080 \
  --hits '2026-09-16T10:00:00+08:00|sl,2026-09-16T12:00:00+08:00|sl'
```

### 3. OKX 网格 amend dry-run（只打印）

打印拟调用的 `amend-order-algo` 字段。`method=PRINT_ONLY`，`will_send_http=false`。默认 `algoId=demo-grid-eth-usdt-001`，**禁止拿去 live amend**。

```bash
uv run python strategies/okx_grid_dry_run.py --help

uv run python strategies/okx_grid_dry_run.py
# 可选：把 JSON 落到文件（仍不发 HTTP）
uv run python strategies/okx_grid_dry_run.py --out /tmp/grid-amend-dry-run.json
```

默认只改绝对价 `slTriggerPx=2150`（不是 Freqtrade 相对比率）。不加仓、不加杠杆。

### 4. 本地 paper 网格成交模拟（`venue=local_paper`）

**免责声明：本地 paper 成交 ≠ OKX Bot 净值；不是收益承诺；只用来观察网格假设与费用后效果。** 无下单、无 amend、无密钥；`lever=1` 现货。

把一段价格路径（合成 / CSV / OKX **公共**行情 K 线）喂给算术网格，模拟格线成交、往返手续费（默认 worst-case）、库存 / PnL、套利次数、买卖次数、SL 触发，并输出与团队 `sim-daily-schema` 同构的日 metrics JSON。

```bash
uv run python tools/local_paper_grid.py --help
```

默认网格参数与演示一致：`ETH-USDT`、`minPx=2200`、`maxPx=3200`、`gridNum=30`、`investment=1000`、`slTriggerPx=2150`、`lever=1`。

#### 4a. 合成振荡路径（离线，确定性，保证有成交）

```bash
# 一天 1440 根 1m K 线：中枢 2700、振幅 ±250、周期 240 步，seed 固定 → 可复现
uv run python tools/local_paper_grid.py --source synthetic --include-fills 10

# 改中枢 / 振幅 / 噪声 / 漂移（负漂移可把价格压到 SL 以下测停机）
uv run python tools/local_paper_grid.py --source synthetic \
  --synthetic-start 2400 --synthetic-amp 120 --synthetic-drift -0.3 --seed 42
```

CI 用这条路径断言 `buy_count/sell_count/arbitrage_num > 0`，避免 Day-1 卡在 0 成交。

#### 4b. OKX 公共行情 K 线（只读 GET，无密钥）

`GET https://www.okx.com/api/v5/market/candles`（每页 ≤300 根，`--pages` 往前翻页）。只拿行情，不碰 Trade / Account 端点。

```bash
# 近一天 5m K 线，同时把 K 线落成 CSV 便于回放
uv run python tools/local_paper_grid.py --source okx-public --bar 5m --limit 288 --pages 1 \
  --save-candles-csv /tmp/eth-usdt-5m.csv \
  --window-label eod --out /tmp/local-paper-eod.json

# 1m K 线走五页（≈25h），再按 CST 窗口裁剪出「今天 00:00–21:00」
uv run python tools/local_paper_grid.py --source okx-public --bar 1m --limit 300 --pages 5 \
  --window-from "2026-09-16 00:00" --window-to "2026-09-16 21:00" --window-label eod
```

#### 4c. CSV 回放（离线、可复现）

CSV 带表头 `ts,open,high,low,close`（多余列忽略；`ts` 可为毫秒 / 秒 / CST 字符串），或无表头的 OKX 原始行 `ts,o,h,l,c,...`。用 `--save-candles-csv` 落下来的文件可原样回放，metrics 与在线下载完全一致。

```bash
uv run python tools/local_paper_grid.py --source csv --candles-csv /tmp/eth-usdt-5m.csv \
  --fills-out /tmp/fills.csv
```

#### 4d. 跑一天 local_paper 并落盘

```bash
# --out-dir 会按 schema 命名：<dir>/YYYY-MM-DD-<runId>.json（日期取窗口结束日，CST）
uv run python tools/local_paper_grid.py --source okx-public --bar 1m --pages 5 \
  --window-from "2026-09-16 00:00" --window-to "2026-09-16 21:00" --window-label eod \
  --run-id local-paper-eth-grid-001 \
  --save-candles-csv /tmp/2026-09-16-eth-1m.csv \
  --out-dir /tmp/sim-out --quiet
```

产物投递位置（团队 workspace，**不在本仓内**，路径仅作说明）：把 `YYYY-MM-DD-<runId>.json` 复制到 `/workspace/okx-team/02-metrics/sim/`，EOD 复盘按 `05-reviews/templates/sim-eod.md` 填写；回撤口径固定 `okx_bot_total_pnl_ratio`。

#### 4e. 输出字段与口径

顶层与 schema 同构：`venue=local_paper`、`mode=模拟`、`algoId=null`、`params`、`window{from,to,label}`、`metrics`、`invalidation_flags`、`notes`。此外附加 `disclaimer`、`will_send_http=false`、`trading_http{order,amend,withdraw=false}`、`data_source`、`fee_model`、`sim_assumptions`、`fills_by_kind`（`--include-fills N` 可内嵌前 N 笔成交）。

| 字段 | 口径 |
|------|------|
| `okx_bot_total_pnl` / `_ratio` | `realized_pnl + unrealized_pnl`（费前），ratio 以 `investment` 为分母；回撤 `max_drawdown_ratio` 按此 ratio 逐根 K 线计算 |
| `grid_profit` | 已完成的格线往返（买低格线→卖高格线）毛利，含启动时市价买入后在上格卖出的部分 |
| `fees_paid_est` | 所有成交手续费（quote 计），默认 worst-case：每笔 `max(maker,taker)`；`--fee-mode maker_taker` 为限价 maker / 市价 taker |
| `fee_after_pnl_est` | 现金记账权益 − investment；恒等于 `okx_bot_total_pnl − fees_paid_est`（CI 断言） |
| `arbitrage_num` | `grid_sell` 成交次数 |
| `sl_hit_count` / `invalidation_hit_count` | SL 触发次数（触发即清仓停机，不重启）；失效条件首次命中的事件数（明细见 `invalidation_events`） |
| `invalidation_flags` | 4 个 schema 标志 + `price_in_band` + `*_ever`（窗口内曾命中） |

模型简化（见输出里的 `sim_assumptions`）：格线精确成交、无滑点 / 最小下单量 / 部分成交、买入手续费从 quote 扣（OKX 实际扣 base）、起始价不在区间内则等待进入区间再启动。**这些简化都意味着结果不能当 OKX Bot 净值。**

### 5. 单测 / 冒烟

```bash
uv run --no-dev python -m unittest discover -s tests -v
```

覆盖：算术格线、费用 worst-case、合成路径必有成交、`total − fees == fee_after` 恒等式、单格往返利润、SL 清仓停机、区间外等待、schema 字段齐全、CSV 往返、CLI 落盘。全部离线。

## CI

GitHub Actions 工作流 [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 在 **pull_request** 以及 **push 到 `master`** 时跑纸面冒烟：

1. `astral-sh/setup-uv@v10.1.0` 安装 uv，`uv sync --locked --no-dev` 同步空运行时（不装交易栈、不装 ruff）。
2. 四个脚本的 `--help` 能通过 `uv run --locked --no-dev python …` 启动（Python 3.12）。
3. `local_paper_grid.py --source synthetic`：断言 `venue=local_paper`、`will_send_http=false`、`lever=1`、`buy/sell/arbitrage > 0`、`total − fees == fee_after`。
4. `python -m unittest discover -s tests`（标准库，无网络）。
5. 各跑一遍默认参数：`okx_grid_dry_run.py` 必须含 `"will_send_http": false` 和 `method: PRINT_ONLY`。

**CI 绿 ≠ 实盘。** 工作流不注入 Secrets、不发 HTTP（CI 里不调 OKX 公共行情，只用合成路径）、不 amend、不提现。**推代码 ≠ live。** 不要在 Actions 里配置 API key / `.env`。

## 笔记

- [`notes/2026-09-16-code-screen-from-reading-pack.md`](notes/2026-09-16-code-screen-from-reading-pack.md) — 哪些可进代码、哪些明确不做
- [`notes/2026-09-16-ft-source-audit.md`](notes/2026-09-16-ft-source-audit.md) — Freqtrade 对照：借什么、不借什么
- [`notes/2026-09-16-v2-tools.md`](notes/2026-09-16-v2-tools.md) — v2 旁路与冒烟口径

## 远程仓与流程

- GitHub：`https://github.com/littleboss/cryptowang`
- 流程：本地草稿 → 本仓（风控执行）→ 运维部署（Atlas）
- **推代码 ≠ 实盘**；改参 / 部署触实盘须用户明确确认
