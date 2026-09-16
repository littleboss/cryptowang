# cryptowang

OKX 策略工程工具箱（纸面 / dry-run）。**推代码 ≠ 实盘。**

本仓只版本化可证伪假设、费用后计算和打印-only 的 amend 草案。没有密钥、没有提现、没有会发单的 HTTP 客户端。Freqtrade 仅作方法参照，**不启用 `freqtrade trade` live**。

## 布局

选的是**仓库根目录**（不套一层 `okx/`），对应原 `07-code/` 骨架：

| 路径 | 内容 |
|------|------|
| `pyproject.toml` / `uv.lock` | uv 工程元数据与锁文件（仅打包，无交易依赖） |
| `strategies/` | 策略与参数 schema、OKX Bot amend **打印**适配、**基线 vs 实验臂 A/B 对照 + 假设评分模块** |
| `tools/` | 纸面校验、观察器、**本地 paper 网格成交模拟器**、**只读 OKX 客户端**（公共行情 GET；可选 `OKX_SIMULATED=1` 只读状态） |
| `fixtures/proposals/` | 提案 JSON（v3：基线 vs B1），供策略模块 / CI 离线消费；无密钥 |
| `tests/` | 标准库 `unittest` 冒烟 / 单测（合成路径必须出成交；只读客户端用本地假服务器，不出网） |
| `notes/` | 筛选结论、Freqtrade 对照、v2 旁路笔记 |
| `backtests/` | 预留：回测脚本与费用后报告（本 PR 未加） |

## 硬门禁

1. **无密钥**：不提交 API key、`.env`、提现权限。`.gitignore` 已挡常见密钥文件。
2. **无提现 / 无跨所转账自动化**。
3. **默认现货 / ≤1x**；`>2x` 标红另批。本仓 dry-run 与 local_paper 固定 `lever=1`。
4. **默认 dry-run**：`will_send_http=false`。未获用户明确确认 + 风控放行前，不得实写 / live amend。允许的网络调用只有两类，且都是 **GET**：(a) 公共行情 K 线 / ticker 的只读 GET（`local_paper_grid.py --source okx-public`、`okx_readonly_client.py ticker|candles`，无签名、无密钥）；(b) `okx_readonly_client.py private-status` 在 **`OKX_SIMULATED=1` + 三个 env 变量齐全** 时对 OKX 模拟盘 账户 / Bot 状态的签名 GET。Trade / amend / transfer / withdraw 端点在代码层被拒绝，不实现。
5. **演示 fixture 的 `algoId`（默认 `demo-grid-eth-usdt-001`）不得用于 live amend。**
6. 纸面结果 **≠ OKX Bot 净值 / 收益承诺**。回撤口径必须用 Bot `total_pnl_ratio`，不是 Freqtrade hyperopt 曲线。
7. 先可证伪假设 → 再写代码 → 费用后回测 / 纸面。优化产出：diff + 前后对比 + 失效条件。

## 当前状态

- 演示提案 v1（`slTriggerPx=2150`）已附条件放行，等用户确认后才允许 dry-run 之外的动作。
- 模拟 venue 当前为 **`local_paper`**（OKX demo key 尚未到位，不接 `okx_demo`）。Day-0 metrics 为 bootstrap（0 新成交）；Day-1 起用 `tools/local_paper_grid.py` 产出。
- 提案 v3（[fixtures/proposals](fixtures/proposals/)）：**立刻不改参**（保持 2200–3200 / 30 / SL 2150 / 1x）；纸面实验臂 **B1**（maxPx 2700、gridNum 20）风控**附条件允许**，只在 `local_paper` 对照跑，用 `strategies/grid_ab_compare.py`；采纳须另行确认。加仓 / 加杠杆已否决（H-C）。
- 只读 OKX 客户端已入库（公共行情 GET；可选 `OKX_SIMULATED=1` 只读状态）。**没有** Trade / amend / withdraw 代码。
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

### 5. 策略模块：基线 vs 实验臂 A/B 对照 + 假设评分（`strategies/grid_ab_compare.py`）

**纸面对照 ≠ OKX Bot 净值 ≠ 收益承诺；任何臂都不会因本模块输出而「采纳」**——输出里 `adoption.adopted=false`、`bot_changed=false` 固定为假，采纳须风控审后用户确认。

模块吃 [提案 v3 JSON](fixtures/proposals/2026-09-16-local-paper-eth-grid-001-v3.json)（`baseline` + 任意 `paper_experiment_*` 臂，当前是 **B1：maxPx 3200→2700、gridNum 30→20，min / SL / 投入 / 1x 不变**），把**同一条 K 线路径、同一费率**喂给 `tools/local_paper_grid.py` 跑每个臂，然后：

- 输出对照 JSON（`--out`）与 markdown 表（`--md-out` / `--print-md`）：`fee_after_pnl_est`、`okx_bot_total_pnl_ratio`、`arbitrage_num`、`max_drawdown_ratio`、`fees_paid_est`、`fee_falsified`、`per_grid_pct`、买/卖比、`untouched_grid_fraction`（从未成交格子占比，对应「上沿闲置」）、失效矩阵（提案 `invalidation` 里的 5 个名字逐项 true/false + `any`）。
- 给可证伪假设打分（状态值只有 `supported_on_window` / `not_yet_falsified` / `falsified*` / `policy_enforced` 等，**不会输出「有效」「盈利」这类结论**）：

| ID | 评分口径（与提案 §2 对齐） |
|----|------|
| H-A | 基线臂 `fee_after_pnl_est > 0` → `supported_on_window`；否则看 `--history-dir` 里 `YYYY-MM-DD-<runId>.json` EOD 序列 + 本次：**连续 7 个 EOD** `fee_after ≤ Day-1（-28.32）` 且 `套利/日 < 1` → `falsified`，不足 7 天 → `not_yet_falsified`（给出 `consecutive_bad_eods` / `days_until_falsifiable`） |
| H-B | 同窗同费下，B1 `Δfee_after ≤ 0` **或** `ΔMDD > 0.02` → `falsified_on_window`（`falsified_by` 列原因）；否则 `supported_on_window`。附 `arb_per_1000_quote`、`untouched_grid_fraction`、`above_max_px_candles` |
| H-C | 加载提案时任何臂 **投入或杠杆高于基线直接拒绝**（`ValueError (H-C)`）；输出每臂 `adds_capital/adds_lever` 与 Day-1 库存风险标志（买/卖比 > 3 且浮亏 → `inventory_risk=true`，即失效项 `buy_sell_ratio_gt_3_with_worsening_float`）；`add_position_proposals_allowed` 固定 false；「强制加仓」压力臂**按设计不构造** |

引擎模式：`--engine import`（默认，进程内调用 `LocalPaperGrid`）或 `--engine subprocess`（把 K 线落成 CSV 后调用 `tools/local_paper_grid.py --source csv` CLI）。两者 metrics 完全一致（单测 + CI 断言）。

```bash
uv run python strategies/grid_ab_compare.py --help

# 离线：合成振荡路径（中枢 2450、±200，同时落在两臂区间内），打印 markdown 表 + 写 JSON
uv run python strategies/grid_ab_compare.py --print-md --quiet --out /tmp/cmp.json

# 用 CLI 子进程引擎，且把每个臂的完整 schema JSON 落盘（<dir>/YYYY-MM-DD-<runId>-<arm>.json）
uv run python strategies/grid_ab_compare.py --engine subprocess --arm-reports-dir /tmp/arms --quiet

# 只跑 B1（基线总会一并跑，作为对照）
uv run python strategies/grid_ab_compare.py --arms B1 --print-md --quiet

# 回放已落盘的 K 线 CSV（与 local_paper_grid --save-candles-csv 同格式）
uv run python strategies/grid_ab_compare.py --source csv --candles-csv /tmp/eth-usdt-5m.csv \
  --md-out /tmp/cmp.md --out /tmp/cmp.json --quiet

# OKX 公共 5m K 线（只读 GET，经 tools/okx_readonly_client.py，无密钥），近 ~25h
uv run python strategies/grid_ab_compare.py --source okx-public --bar 5m --limit 300 --pages 1 \
  --save-candles-csv /tmp/eth-5m.csv --print-md --quiet --out /tmp/cmp-public.json

# H-A 的 7 日滚动规则：把 02-metrics/sim/ 的 EOD 文件目录喂进去
uv run python strategies/grid_ab_compare.py --source csv --candles-csv /tmp/today.csv \
  --history-dir /workspace/okx-team/02-metrics/sim --print-md --quiet
```

安全阀：提案 `will_send_http` 不为 `false` → 拒绝加载；任何臂 `lever≠1`、`slTriggerPx ≥ minPx` → 拒绝；实验臂投入 / 杠杆高于基线 → 拒绝（H-C）。模块本身不发任何交易 HTTP；`--source okx-public` 只走公共行情 GET。

### 6. 只读 OKX 客户端（`tools/okx_readonly_client.py`）

**只有 GET。** 不实现、也不会实现：下单、amend / stop 网格、划转、提现。代码层三道闸：`assert_read_only()` 在任何 socket 打开前拒绝非 GET 方法、拒绝不在只读白名单里的路径、拒绝含 `/trade/`、`amend`、`order-algo`、`withdraw`、`transfer`、`/asset/` 等片段的路径；`place_order/amend_algo/transfer/withdraw` 方法存在但只会抛 `ReadOnlyViolation`。

**公共行情（无密钥）**

```bash
uv run python tools/okx_readonly_client.py --help
uv run python tools/okx_readonly_client.py ticker --inst-id ETH-USDT
uv run python tools/okx_readonly_client.py candles --inst-id ETH-USDT --bar 5m --limit 288 --pages 1 \
  --csv-out /tmp/eth-usdt-5m.csv      # 可直接喂 local_paper_grid / grid_ab_compare --source csv
uv run python tools/okx_readonly_client.py policy   # 离线打印只读策略 + env 变量是否设置（只显示长度，不显示值）
```

**可选：OKX 模拟盘只读状态（签名 GET）**

仅当以下四个环境变量**全部**满足时才会激活，否则 `private-status` 打印 `{"skipped": true, ...}` 并以 0 退出（CI 就是这个分支）：

```bash
export OKX_API_KEY=...          # 模拟盘 key（在 OKX「模拟交易」里创建，只勾读取权限，不要提现权限）
export OKX_API_SECRET=...
export OKX_API_PASSPHRASE=...
export OKX_SIMULATED=1          # 缺这个 → 直接拒绝（退出码 3），不会用 live key 发任何请求

uv run python tools/okx_readonly_client.py private-status --ccy USDT
uv run python tools/okx_readonly_client.py private-status --algo-id <demoAlgoId>   # 附 Bot 详情摘要
```

请求头带 `x-simulated-trading: 1`；只调 `GET /account/balance|config`、`GET /tradingBot/grid/orders-algo-pending|history|details|positions`。`bot_status_summary()` 把 Bot 详情映射到 sim-daily-schema 字段（`okx_bot_total_pnl_ratio`、`grid_profit`、`float_profit`、`arbitrage_num`，`venue=okx_demo`），方便日后把 `okx_demo` 与 `local_paper` 同构对照。

密钥纪律：只从环境变量读；`repr` / 日志只显示 `<set:N chars>`；不写文件；`.gitignore` 已挡 `.env*`。**不要把密钥放进 GitHub Actions Secrets**——CI 不需要。

### 7. 单测 / 冒烟

```bash
uv run --no-dev python -m unittest discover -s tests -v
```

覆盖：算术格线、费用 worst-case、合成路径必有成交、`total − fees == fee_after` 恒等式、单格往返利润、SL 清仓停机、区间外等待、schema 字段齐全、CSV 往返、CLI 落盘；策略模块的提案加载 / 三道拒绝（`will_send_http=true`、`lever=2`、加仓臂）、两臂同路径出成交、必填输出字段、deltas 一致性、H-A 7 日连败 / 重置、H-B 2pct MDD 边界、Day-1 库存风险规则、import 与 subprocess 引擎一致、CLI 落 JSON + markdown；只读客户端的 GET-only 闸门、trade/amend/withdraw 路径拒绝、live key 拒绝、`repr` 不泄露、HMAC 签名向量、假服务器上的 ticker / 分页 candles / 签名 GET 头、无密钥 stub。全部离线（客户端测试用本机 `http.server` 假 OKX）。

## CI

GitHub Actions 工作流 [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 在 **pull_request** 以及 **push 到 `master`** 时跑纸面冒烟：

**job `paper-smoke`（离线，阻塞）**

1. `astral-sh/setup-uv@v10.1.0` 安装 uv，`uv sync --locked --no-dev` 同步空运行时（不装交易栈、不装 ruff）。
2. 六个脚本的 `--help` 能通过 `uv run --locked --no-dev python …` 启动（Python 3.12）。
3. `local_paper_grid.py --source synthetic`：断言 `venue=local_paper`、`will_send_http=false`、`lever=1`、`buy/sell/arbitrage > 0`、`total − fees == fee_after`。
4. `grid_ab_compare.py` 用 `import` 与 `subprocess` 两种引擎各跑一次合成路径：断言两臂 `baseline`/`B1` 都在、`lever=1`、必填字段齐全、`arbitrage_num > 0`、两臂 K 线数一致、两引擎 metrics 相等、`adopted=false`、`bot_changed=false`、H-A/H-B/H-C 都有评分、H-C 无否决臂且 `add_position_proposals_allowed=false`；并把 markdown 表打到日志。
5. `okx_readonly_client.py private-status` 在清空 `OKX_*` 环境后必须打印 `skipped=true`（无密钥 stub），`policy` 里 `will_send_http/order/amend/withdraw/transfer` 全为 false。
6. `python -m unittest discover -s tests`（标准库，无网络）。
7. 各跑一遍默认参数：`okx_grid_dry_run.py` 必须含 `"will_send_http": false` 和 `method: PRINT_ONLY`。

**job `public-read-smoke`（出网，`continue-on-error: true`，不阻塞）**

对 OKX **公共** `ticker` / `candles` 做只读 GET（无密钥；`OKX_*` 显式置空），断言 `auth=none`、`read_only=true`、K 线按时间升序；再把这 12 根公开 K 线经 CSV 回放喂给 `grid_ab_compare.py`。这一步红只代表「runner 到 OKX 公共 API 不通」，不代表任何交易发生。

**CI 绿 ≠ 实盘。** 工作流不注入 Secrets、不 amend、不提现、不下单；唯一出网的是上面那个只读公共行情 GET。**推代码 ≠ live。** 不要在 Actions 里配置 API key / `.env`。

## 笔记

- [`notes/2026-09-16-code-screen-from-reading-pack.md`](notes/2026-09-16-code-screen-from-reading-pack.md) — 哪些可进代码、哪些明确不做
- [`notes/2026-09-16-ft-source-audit.md`](notes/2026-09-16-ft-source-audit.md) — Freqtrade 对照：借什么、不借什么
- [`notes/2026-09-16-v2-tools.md`](notes/2026-09-16-v2-tools.md) — v2 旁路与冒烟口径

## 远程仓与流程

- GitHub：`https://github.com/littleboss/cryptowang`
- 流程：本地草稿 → 本仓（风控执行）→ 运维部署（Atlas）
- **推代码 ≠ 实盘**；改参 / 部署触实盘须用户明确确认
