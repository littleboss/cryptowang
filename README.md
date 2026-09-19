# cryptowang

OKX 策略工程工具箱（纸面 / dry-run）。**推代码 ≠ 实盘。**

本仓只版本化可证伪假设、费用后计算和打印-only 的 amend 草案。没有密钥、没有提现、没有会发单的 HTTP 客户端。Freqtrade 仅作方法参照，**不启用 `freqtrade trade` live**。

## 布局

选的是**仓库根目录**（不套一层 `okx/`），对应原 `07-code/` 骨架：

| 路径 | 内容 |
|------|------|
| `pyproject.toml` / `uv.lock` | uv 工程元数据与锁文件（唯一第三方运行时依赖：官方 `python-okx==0.4.4`，只经只读门面可达） |
| `strategies/` | 策略与参数 schema、OKX Bot amend **打印**适配、**基线 vs 实验臂 A/B 对照 + 假设评分模块**、**只读纸面多腿套利扫描器（Phase A，研究侧支）**、**只读纸面对冲组合评分（Phase B：B1/B2/B3，相对价值，研究侧支）**、**只读纸面现货三角扫描器（菜单 T1：ETH-USDT / BTC-USDT / ETH-BTC，`same_venue_microstructure`，研究侧支）**、**只读纸面组合扩搜 C1–C4（`paper_combo_expansion.py`：少腿 / 深簿 / 可证伪，净边只出自 Cost Engine，研究侧支）**、**E2 Phase A demo OMS（`e2_demo_oms.py`：E2-G1 ETH-USDT 现货分层限价梯意向构建器，纯 dry-run，无 HTTP 代码）** |
| `tools/` | 纸面校验、观察器、**本地 paper 网格成交模拟器**、**只读 OKX 客户端**（标准库后端 `okx_readonly_client.py`；官方 SDK 只读门面 `okx_sdk_readonly.py`；公共行情 / 盘口 / funding GET，可选 `OKX_SIMULATED=1` 只读状态）、**Cost Engine v0**（`cost_engine.py`：全成本 / 净边 / breakeven funding 的可复用纸面算术，A/B/T1/C1–C4 扫描器共用，研究侧支） |
| `fixtures/proposals/` | 提案 JSON（v3：基线 vs B1），供策略模块 / CI 离线消费；无密钥 |
| `fixtures/arb_books/` | 合成盘口快照 fixture（spot / perp / 期权 + funding），供套利扫描器 / 组合评分离线回放 / CI；Phase B 用双到期版本；T1 用三腿现货簿版本（各簿带自己的时间戳）；扩搜版另带**两个交割合约簿 + funding 历史**（`…-eth-expansion-books-sample.json`）；**非行情证据** |
| `fixtures/cost_engine/` | Cost Engine v0 的 JSON 用例（手算数字 + 拒绝用例：年化毛边、mark 作可执行价、买腿用 bid、单边簿、无依据 `calibrated=true`），供单测 / CI 离线回放；**非行情证据** |
| `tests/` | 标准库 `unittest` 冒烟 / 单测（合成路径必须出成交；只读客户端用本地假服务器，不出网） |
| `notes/` | 筛选结论、Freqtrade 对照、v2 旁路笔记 |
| `backtests/` | 预留：回测脚本与费用后报告（本 PR 未加） |

## 硬门禁

1. **无密钥**：不提交 API key、`.env`、提现权限。`.gitignore` 已挡常见密钥文件。
2. **无提现 / 无跨所转账自动化**。
3. **默认现货 / ≤1x**；`>2x` 标红另批。本仓 dry-run 与 local_paper 固定 `lever=1`。
4. **默认 dry-run**：`will_send_http=false`。未获用户明确确认 + 风控放行前，不得实写 / live amend。允许的网络调用只有两类，且都是 **GET**：(a) 公共行情 K 线 / ticker 的只读 GET（`local_paper_grid.py --source okx-public`、`okx_readonly_client.py ticker|candles`，无签名、无密钥）；(b) `okx_readonly_client.py private-status` / `okx_sdk_readonly.py private-status` 在 **`OKX_SIMULATED=1` + 三个 env 变量齐全** 时对 OKX 模拟盘 账户 / Bot 状态的签名 GET。Trade / amend / transfer / withdraw 端点在代码层被拒绝，不实现。官方 SDK（`python-okx`）只允许以 **GET-only 子集** 出现：不 import `okx.Trade` / `okx.Funding` / `okx.SubAccount`，SDK 自带的 POST 方法在被门面继承后一律抛 `ReadOnlyViolation`（见 §6b）。
5. **演示 fixture 的 `algoId`（默认 `demo-grid-eth-usdt-001`）不得用于 live amend。**
6. 纸面结果 **≠ OKX Bot 净值 / 收益承诺**。回撤口径必须用 Bot `total_pnl_ratio`，不是 Freqtrade hyperopt 曲线。
7. 先可证伪假设 → 再写代码 → 费用后回测 / 纸面。优化产出：diff + 前后对比 + 失效条件。

## 当前状态

- 演示提案 v1（`slTriggerPx=2150`）已附条件放行，等用户确认后才允许 dry-run 之外的动作。
- 模拟 venue 当前为 **`local_paper`**（OKX demo key 尚未到位，不接 `okx_demo`）。Day-0 metrics 为 bootstrap（0 新成交）；Day-1 起用 `tools/local_paper_grid.py` 产出。
- 提案 v3（[fixtures/proposals](fixtures/proposals/)）：**立刻不改参**（保持 2200–3200 / 30 / SL 2150 / 1x）；纸面实验臂 **B1**（maxPx 2700、gridNum 20）风控**附条件允许**，只在 `local_paper` 对照跑，用 `strategies/grid_ab_compare.py`；采纳须另行确认。加仓 / 加杠杆已否决（H-C）。
- 只读 OKX 客户端已入库（公共行情 GET；可选 `OKX_SIMULATED=1` 只读状态）。**没有** Trade / amend / withdraw 代码。
- 官方 SDK `python-okx==0.4.4` 已作为 **只读子集** 依赖入库（`tools/okx_sdk_readonly.py`）：与标准库客户端同一套 CLI / JSON 契约，仅 GET；写路径在开 socket 之前抛错。**仍然不接 `okx_demo` venue，仍然 `will_send_http=false`。**
- 纸面多腿套利扫描器 Phase A（`strategies/paper_arb_scanner.py`）已入库：**研究侧支，只读 / observe_only**，不替换网格主线；风控附条件通过（见 §8），live execution **不在**批准范围。
- 纸面对冲组合评分 Phase B（`strategies/paper_combo_scanner.py`，B1 备兑 carry / B2 日历 vol / B3 带翼 25Δ RR）已入库：**研究侧支，只读 / observe_only / 全部 `relative_value`**，与 A 扫描器并列，不并入网格；风控附条件通过「只读指标 + 纸面成交」（见 §9），B2 delta 对冲**仅纸面模拟**，live 不在范围。Phase A 首扫净边全负 → B 是「显式 RV 组合研究」，不是「找回边」。
- Cost Engine v0（`tools/cost_engine.py`，路线图 P0 / Phase C0）已入库：**纸面只读、observe_only、`calibrated=false`**。把 A/B 记录里的 `costs_bps` 算术升格为共用模块，输出 `all_in_cost_bps` / `net_edge_bps` / `breakeven_funding_rate` / 分量拆解；A/B 扫描器每条记录附 `cost_engine` 交叉校验块（须与 `costs_bps.total` 一致，`--no-cost-engine` 可关），`costs_bps` / `passes_threshold` 本身**不变**。拒绝 `current_funding × 365` 当净边，年化字段一律 `_ref` 展示（见 §10）。**不含** Fair-value（C1）/ Funding expectation（C3）/ Score schema / Execution；`04-risk` 附条件放行仅覆盖纸面工程。
- 纸面现货三角扫描器 T1（`strategies/paper_triangle_scanner.py`，菜单 `demo-strategy-menu-stable-edge-v1` 的 T1，`04-risk` 同日**附条件放行只读 / paper**）已入库：**研究侧支，只读 / observe_only / `taxonomy=same_venue_microstructure`（禁 `risk_free`）**，白名单固定 `ETH-USDT` · `BTC-USDT` · `ETH-BTC` 双向环；三腿簿时间戳偏差 > 200 ms → `stale_book` 不计 pass；每腿半点差 > 15 bp → `illiquid`；**毛边 / 净边 / 成本拆解全部来自 `tools/cost_engine.py evaluate_legs`**（模块自己不算净边）；`residual_risks` 必含非原子三腿 / 残留库存。汇总只报 `net_positive_rate` / `cost_kill_rate` / 纸面合成成交率（见 §11）。**本批不授权任何 demo 下单**；不并入网格主线。
- 组合扩搜 C1–C4（`strategies/paper_combo_expansion.py`）已入库：**研究侧支，只读 / observe_only / `phase=C` / 全部 `relative_value` / `calibrated=false`**。起点是 Cost Engine 复算 A+B n=95 → net>0 = 0%：不加腿、不加杠杆，改找**更少腿 / 更深簿 / 可证伪**的纸面候选。C1 现货 vs 交割合约基差（≤2 腿，远月 / perp 仅 `hedge_note`）、C2 funding 期望**路径** + 基差确认（禁 `current×365`）、C3 B1/B3 采样带**对称外扩一档**（Δ / moneyness ±5pp）后再过 Cost Engine 且 **N≥5 才许写结论**、C4 A2/A3 流动性硬滤（半价差 ≤25 bp · ≥1 张 · ≥50 USDT）。**一切净边只来自 `tools/cost_engine.py evaluate`**；默认 ETH，BTC 须 `--allow-btc`（本批未放行）；A/B 扫描器保留不替换（见 §12）。`04-risk` 附条件放行仅覆盖只读扫描 + 复算，**不含任何 live**。
- E2 Phase A demo OMS（`strategies/e2_demo_oms.py`，首个策略标签 **E2-G1** = ETH-USDT 现货分层限价梯）已入库：**仅连通性 dry-run**——构建 ≤10 档 / ≤100 USDT 的挂单买梯 **意向 JSON**，`will_send_http` 默认 `false` 且在 Phase A **一律拒绝**为 `true`（无 demo env → `DemoEnvCheckFailed`；有 demo env 仍 → `PhaseASendNotImplemented`）；模块内没有任何 HTTP / SDK import，`place_order` 是显式抛错。**Phase B（真实 demo 下单）不在本 PR：须另开 `04-risk` + 用户明确确认。** 官方网格 Bot A / B′ / C 不动（见 §13）。
- 本仓只入库纸面工具；**push ≠ 实盘**。

## 包装说明（仅工程，不是实盘）

本仓用 [`uv`](https://docs.astral.sh/uv/) + `pyproject.toml` 管理 Python 环境。**这只是打包 / 依赖锁定，不是开通实盘。**

- 运行时依赖只有一个第三方包：官方 OKX SDK `python-okx==0.4.4`（精确 pin，连带 `httpx[http2]` / `requests` / `websockets` / `loguru` 等传递依赖，全部锁在 `uv.lock`）。它**只**通过 `tools/okx_sdk_readonly.py` 这层 GET-only 门面可达；`okx.Trade` 等写模块在整个仓库内没有任何 import（CI 用 AST 扫描断言）。其余脚本仍是标准库，`okx_readonly_client.py` 标准库后端原样保留。
- `uv sync` / `uv run` **≠** 下单、**≠** 注入密钥、**≠** 启用 freqtrade live。装上 SDK ≠ 拥有下单能力——门面把 SDK 的 POST 方法全部替换为抛错。
- 脚本保持在 `tools/` 与 `strategies/`，用 `uv run python …` 直接跑；没有把本仓装成可发单包。

## 如何运行

先装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，再同步环境（会按 `uv.lock` 建 `.venv`，装入 pin 死的 `python-okx`；标准库脚本不依赖它）：

```bash
uv sync
```

开发工具（可选，`dependency-groups.dev`，目前只有 ruff）：

```bash
uv sync --group dev
# 或默认也会装上 dev 组：uv sync
```

标准库脚本（含 `okx_readonly_client.py`）仍可用系统 `python3` 直接跑；只有 `okx_sdk_readonly.py` 需要 `.venv` 里的 `python-okx`。推荐统一用 `uv run`，与 CI 一致。

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

公共只读端点（无密钥，供 §8 扫描器使用）：`GET /market/books`（盘口，`sz` 档；SWAP / OPTION 的 size 是**张数**，调用方按 `ctVal` 折成币）、`GET /public/funding-rate`、`GET /public/instruments`（`ctVal` / 行权价 / 到期）、`GET /market/tickers`。仍受同一道 `assert_read_only()` 闸门约束。

### 6b. 官方 SDK 只读子集（`tools/okx_sdk_readonly.py`，依赖 `python-okx==0.4.4`）

**为什么现在引入官方 SDK**：后续要对照 `okx_demo` 与 `local_paper` 时，希望端点常量、签名、`x-simulated-trading` 头都跟 OKX 官方实现（[okxapi/python-okx](https://github.com/okxapi/python-okx)）对齐，减少手写 HTTP 层与官方漂移的风险；同时把「装了 SDK 会不会顺手就能下单」这个问题在代码层封死，而不是靠约定。它是标准库客户端的**同契约替代后端**，不是新能力：同样的 `ticker | candles | private-status | policy` 子命令、同样的 JSON 字段、同样的退出码（无密钥 stub → 0，live key → 3）。

**只读子集怎么实现**（三层闸，缺一不可）：

1. **import 层**：整个文件只 import `okx.MarketData` / `okx.Account` / `okx.Grid` 三个读向模块；`okx.Trade`、`okx.Funding`、`okx.SubAccount`、`okx.Convert`、`okx.websocket` 等列入 `FORBIDDEN_SDK_MODULES`，`policy` 子命令与单测 / CI 都会检查 `sys.modules` 与源码 AST，出现即失败。
2. **请求漏斗层**：SDK 所有 REST 调用都经过 `OkxClient._request(method, path, params)`，而 `OkxClient` 本身是 `httpx.Client`，所以还会经过 `send()`。门面用 `GatedMarketAPI / GatedAccountAPI / GatedGridAPI` 子类同时覆写这两处，复用 `okx_readonly_client.assert_read_only()`：非 GET、或路径不在只读白名单、或命中 `/trade/`、`amend`、`order-algo`、`withdraw`、`transfer` 等片段 → 在开 socket 前抛 `ReadOnlyViolation`。于是 SDK 自带的 `grid_amend_order_algo()`、`grid_stop_order_algo()`、`grid_order_algo()`、`set_leverage()`、`grid_withdraw_income()` 等 **POST 方法即便被调用也只会抛错**；`post/put/patch/delete` 也被替换为直接抛错。单测会**遍历 SDK 类上所有方法**逐个调用来证明这点，SDK 升级新增写方法也会被自动覆盖。
3. **门面层**：`OkxSdkPublicClient` / `OkxSdkReadOnlyPrivateClient` 不暴露 SDK 对象，只提供显式读方法（`get_ticker/get_candles`、`get_balance/get_account_config/get_grid_pending|history|details|positions`）；`place_order/amend_algo/stop_algo/transfer/withdraw` 只存在于「抛错」形态。

白名单是**同一份**：门面直接 import `okx_readonly_client` 的 `PUBLIC_READ_PATHS` / `PRIVATE_READ_PATHS`，所以 §6 为扫描器放开的 `books` / `funding-rate` / `instruments` / `tickers` 在 SDK 漏斗层同样被允许（仍是无密钥 GET），但门面**不**为它们提供读方法——§8 / §9 扫描器继续走标准库客户端；私有白名单未变。

**公共行情（无密钥）**

```bash
uv run python tools/okx_sdk_readonly.py --help
uv run python tools/okx_sdk_readonly.py ticker --inst-id ETH-USDT
uv run python tools/okx_sdk_readonly.py candles --inst-id ETH-USDT --bar 5m --limit 288 --csv-out /tmp/eth-5m.csv
uv run python tools/okx_sdk_readonly.py policy    # 离线：策略 + 已 import / 已加载的 okx 模块审计 + env 是否设置（只显示长度）
```

**可选：开启 OKX 模拟盘只读状态（签名 GET）**——与 §6 完全相同的四个环境变量，缺 `OKX_SIMULATED=1` 直接拒绝（退出码 3）；SDK 的 `flag`（`x-simulated-trading`）在私有路径硬编码为 `"1"`，构造函数不接受其他值：

```bash
export OKX_API_KEY=...            # 在 OKX「模拟交易」里创建，只勾读取权限
export OKX_API_SECRET=...
export OKX_API_PASSPHRASE=...
export OKX_SIMULATED=1
uv run python tools/okx_sdk_readonly.py private-status --ccy USDT [--algo-id <demoAlgoId>]
```

**硬门禁（评审按此拒收）**：无 Trade / 下单 / amend algo / 划转 / 提现可调路径；公共端点优先、无密钥；私有路径仅 GET、仅模拟盘；密钥只从 env 读、`repr` / CLI / SDK 日志都不打印（SDK 的 loguru `okx` 命名空间在 import 时即禁用，`debug` 永远为 False）；`will_send_http` 恒为 `false`；不新增任何 amend HTTP 客户端；只限现货 / ≤1x 语境。

### 7. 单测 / 冒烟

```bash
uv run --no-dev python -m unittest discover -s tests -v
```

覆盖：算术格线、费用 worst-case、合成路径必有成交、`total − fees == fee_after` 恒等式、单格往返利润、SL 清仓停机、区间外等待、schema 字段齐全、CSV 往返、CLI 落盘；策略模块的提案加载 / 三道拒绝（`will_send_http=true`、`lever=2`、加仓臂）、两臂同路径出成交、必填输出字段、deltas 一致性、H-A 7 日连败 / 重置、H-B 2pct MDD 边界、Day-1 库存风险规则、import 与 subprocess 引擎一致、CLI 落 JSON + markdown；只读客户端的 GET-only 闸门、trade/amend/withdraw 路径拒绝、live key 拒绝、`repr` 不泄露、HMAC 签名向量、假服务器上的 ticker / 分页 candles / 签名 GET 头、无密钥 stub；官方 SDK 门面的模块审计（`okx.Trade` 等未加载、源码 import 子集）、**遍历 SDK 全部方法**证明 POST 方法与非白名单 GET 都在 `httpx.send` 之前抛错（`unittest.mock` 挂在 `httpx.Client.send` 上断言从未被调用）、`post/put/patch/delete/request/stream/send` 写动词抛错、`flag=1` 强制、loguru 不泄露头、假服务器上的无密钥 ticker / 分页 candles / 带 `x-simulated-trading: 1` 的签名 GET、与标准库客户端输出逐字段一致、CLI stub / live key 拒绝 / 不回显密钥；套利扫描器的硬门禁（mark/mid/last 作可执行价 → 拒绝；`action≠observe_only` / `will_send_http=true` → 拒绝；A1 非 `relative_value` → 拒绝；裸卖期权 → 拒绝；源码无 `/api/v5/trade` / `urlopen`）、A1 / A2 / A3 手算数字（funding × H、PCP conversion / reversal、box 权利金与隐含利率）、币本位权利金保守折算、perp 代理降级标注 + funding 计入成本、持续度连续样本 / 时长 / 重置、薄簿 `illiquid` / `insufficient_depth` / impact、safety_buffer = 分量之和、paper fill 块、fixture 回放全字段 + 卖箱永不 pass、CLI JSONL / summary、假 OKX 公共源（张数 × `ctVal`、行权价挑选、全部 GET）；Phase B 组合评分的硬门禁（`phase≠B` / `taxonomy≠relative_value` / `combo_id` 不匹配 / `hedge_mode` 越权 / 残留风险缺项 / 含 `risk_free`、`无风险`、`稳赚`、`guaranteed` 字样 / `live_hedge_http=true` / 裸卖 → 全部拒绝）、备兑规则（B1 短 call 须由 **spot** 名义覆盖、反向日历拒绝、无翼 RR 拒绝且不建记录）、Black-76 平价 / delta / 隐含波动率往返、B1 / B2 / B3 手算数字（备兑权利金边 = bid − ATM 参照公平价、funding × H、日历可执行价差 vs 平坦期限参照、RR 可执行偏离 × vega）、B1 负 funding 记成本 / 预测翻号失效、Δ 带 → moneyness 兜底、B2 纸面 delta 对冲成本（多头 perp 付 funding、收侧不记）、B3 滚动参照未满样本 → 失效、`--b3-paper-delta` 备选模式、buffer 含 `vol_path_haircut`、fixture 回放（三家族齐全、B1 / B2 越过 buffer → 持续度 → pass 路径、B3 越过但不 pass、A1 同窗对照、summary 无禁语）、Phase A fixture 兼容（单到期 → 零记录 + skipped 计数）、CLI、假 OKX 公共源全 GET；Cost Engine v0 的硬门禁（`action≠observe_only` / `will_send_http=true` / `annualized=true` / `tradable_claim_allowed=true` / 禁语 → 拒绝；`LegSpec` 用 mark / mid / last、买腿用 bid、卖腿用 ask、单边簿 → 拒绝；`gross_basis` 非持有期（annualized / apy / x365）→ 拒绝；`FundingLeg` 期数超一年 → 拒绝；`calibrated=true` 无依据 → 拒绝；源码无 `urllib` / `socket` / trade 路径且不 import 扫描器与网格模块）、八个分量原语手算数字（期权费用上限 + 结算费、半点差只记额外 crossings、VWAP 冲击、借币 / 转账 / 资本机会成本 / funding 不确定 / 对冲）、`leg_costs` 从 bid/ask 腿出 fees / spread / impact（含币本位 `quote_conv`）、双计拒绝、`all_in = Σ 四位小数分量`、`net = gross − all_in`、**breakeven 恒等式**（`headroom × H ≈ net`；把 breakeven 喂回去净边 ≈ 0；+1 bp → +H bp）、funding 记成本 + 其它毛边的 breakeven、无 funding / H=0 → `None` + reason、缺项 / 未知项 / 负项打 flag 不丢、`FundingLeg` 收付方向 / `min(|now|,|next|)` / 预测翻号归零、summary 聚合、fixture 用例全数复现 + 拒绝用例逐个命中、CLI 落盘 / 不匹配退出码 1 / `--help`；A/B 扫描器接线（每条记录 `cost_engine.all_in_cost_bps == costs_bps.total`、`net` 一致、A1 / B1 有 breakeven 且恒等式成立、A2 / A3 / B2 / B3 为 `None`、`--no-cost-engine` 只去掉该块其余字段逐项相等、`finalize_*` 对被改动的块拒绝、B 块必含 `vol_path_haircut`、A1 对照臂同开关）；组合扩搜 C1–C4 的硬门禁（`action≠observe_only` / `will_send_http=true` / `taxonomy≠relative_value` / `phase≠C` / `combo_id` 不以 `expansion_id` 开头 / `calibrated=true` / `net_edge_source` 非 cost engine / `cost_engine` 块缺失或与记录 net / gross / all_in 不一致或标 annualized / tradable / 禁语 / mark 作可执行价 / C1 三腿 / `hedge_note` 可交易 / stub 模型改名 / fair-value 进净边 / 空现货缺 `spot_borrow` 残留 / C2 `annualized_used_in_net_edge=true` → `NaiveAnnualizationRefused` / 基差冲突不失效 / `liquidity_ok=false` 却 pass 或缺 flag / 失效却 pass / C3 非 B1/B3 / C4 非 A2/A3 或叠第四腿 / 去掉 spot 腿的 B1 → 裸卖拒绝；源码无 trade / `urlopen` / `POST` / `* 365` / `* 1095`、含 `ce.evaluate(`、无 `gross_bps -` 旁路）、ETH 默认 / BTC 须 `--allow-btc` / 非 ETH 快照拒绝、风控标定锁死（半价差 >25、张数 <1、名义 <50、N<5、其它机制或步长、`calibrated=true` buffer → 全部拒绝）、放宽**恰好一档**（Δ `[0.15,0.25]→[0.10,0.30]`、moneyness `[0.05,0.30]→[0.0,0.35]`、B3 容差 `0.10→0.15`，其余字段逐项相等）、C4 滤镜（宽点差腿被丢 / 小量 / 小名义 / 单边簿，口径为标的名义 bps 且币本位权利金按 spot 折算，`contract_size_base` 换算）、C2 stub 路径（H 期求和而非 ×1095、衰减向锚收敛、历史均值 / pstdev 锚、预测翻号归零、负 funding 换边、期数超一年拒绝）与基差确认三态、C1 手算（两腿 bid/ask、毛边 = future_bid − spot_ask、费用 = 2 spot + future taker + 交割费、半点差只记 spot 出场、`hedge_note` 含远月 + perp 且不可交易、曲线 `basis_apr_ref` 仅展示、fair stub = S_mid、`--c1-tradeable-expiries 2` / `--c1-hold horizon` 路径、贴水 → 反向 + 借币残留 + 无借币失效 / 有借币记成本、无交割合约 → skipped）、C2 手算（毛边 = 路径和、breakeven 恒等式、基差冲突失效、负 funding 空现货须借币、缺 funding / perp → 空）、fixture 回放（四族齐全且全部过 finalize、C1 六快照三条路径、C2 六快照末条冲突、C3 B1 18 条 vs 默认带 0 / B3 12 vs 12、`sample_coverage` 递增、C4 宽腿 / 小量 / 深 ITM 被标且永不 pass、`--c4-mode skip` 条数 = 通过数、Phase B fixture 无交割合约 → C1 skipped 其余照跑、A/B 扫描器在扩搜 fixture 上仍能跑）、summary（`falsifiable_metrics` 四项、覆盖基线 / 增量、两种 cost_kill 口径、结论字串无禁词、N 抬高 → 全部 `coverage_insufficient_no_verdict`、单快照 B1 n=3 → 无结论）、CLI（`evaluate-fixture` 落 JSONL + summary + stderr 指标、默认 `scan` + `--only-exceeding` + `--print-summary`、`policy`、`--help`、BTC 无 flag 退出 1、放宽风控标定退出 1）、假 OKX 公共源（另供 FUTURES 元数据 + 交割合约盘口，张数 × `ctVal`，全部 GET，无 POST）。全部离线（客户端 / 扫描器测试用本机 `http.server` 假 OKX）。

### 8. 只读纸面多腿套利扫描器 · Phase A（`strategies/paper_arb_scanner.py`）

**研究侧支，不替换现货网格 / `local_paper` 主线。** 对应提案 `paper-multi-leg-arb-scanner-v1`（`03-proposals/2026-09-16-paper-multi-leg-arb-scanner-v1.md`）与 `04-risk` 附条件通过意见。**只读 / 纸面 / observe_only；合 PR ≠ 放行下单；live execution 不在批准范围。**

代码层硬门禁（都有单测）：

| 门禁 | 实现 |
|------|------|
| `action` 恒 `observe_only`，`will_send_http` 恒 `false` | 模块常量 + `finalize_record()` 校验，改了就抛 `ObserveOnlyViolation` |
| 可执行价只能是 bid / ask | `Leg.__post_init__`：买腿必须 `ask`、卖腿必须 `bid`；`mark` / `mid` / `last` 直接拒绝。mark 只作 `mark_price_ref` / `mark_ref_only` 对照字段 |
| 零 Trade / 下单 / amend / 提现 / 划转 | 唯一网络路径是 `tools/okx_readonly_client.py` 的公共 GET（books / funding-rate / instruments）；单测断言源码不含 `/api/v5/trade`、`urlopen`、`POST` |
| taxonomy | A1 强制 `relative_value`；A2 / A3 强制 `identity_approx`；A2 用 perp 当远期锚时降级标注为 `identity_approx_with_perp_proxy`（`taxonomy_base` 保留 `identity_approx`），且 funding 期望并入 `costs_bps.funding_expected` |
| 杠杆概念 1x；不裸卖期权 | `leverage_concept=1`；`assert_no_naked_short_options()`：卖 call 须有多头标的或同类多头期权，卖 put 须有空头标的或同类多头期权，否则拒绝建记录。**卖箱**（short box）会评估但打 `short_box_margin_not_approved` 并永不 `passes_threshold`（期权保证金卖箱须另批） |
| 无收益承诺 | 每条 RV 记录带 `relative_value_not_riskless`，identity 记录带 `identity_approx_not_riskless`；summary 状态值只有 `no_pass_on_window` / `not_yet_falsified_on_window` / `insufficient_samples_for_persistence` / `no_samples` |

**三个家族（同所、默认 OKX 概念）**

| ID | family | taxonomy | 腿（买 @ask / 卖 @bid） | 毛边 |
|----|--------|----------|--------------------------|------|
| A1 | `A1_funding_carry` | `relative_value` | 正费率：买 spot @ask + 卖 perp @bid；负费率反向（标 `requires_spot_borrow`，无借币利率则 `borrow_unavailable` → 不 pass） | `min(\|f_now\|, \|f_next\|) × H × N`；不利入场基差计入，**有利基差默认不记**（`--credit-favorable-basis` 才记）；预测费率翻号 → `funding_sign_flip_predicted`，funding 记 0 |
| A2 | `A2_pcp_conversion` / `A2_pcp_reversal` | `identity_approx`（perp 锚 → `identity_approx_with_perp_proxy`） | conversion：买标的 @ask + 买 put @ask + 卖 call @bid；reversal：卖标的 @bid + 卖 put @bid + 买 call @ask | `K·e^{-rT} − (S_ask + P_ask − C_bid)`；reversal 取反。币本位权利金保守折算：付币按 spot ask、收币按 spot bid（`option_premium_in_base_ccy_converted_at_spot_bid_ask`） |
| A3 | `A3_box` | `identity_approx` | 买箱：买 C(K1) @ask + 卖 C(K2) @bid + 买 P(K2) @ask + 卖 P(K1) @bid；卖箱取反 | `(K2−K1)·e^{-rT} − π_exec`，并反解 `implied_box_rate_apr` 与 `ref_rate_apr` 之差 |

`bps` 一律相对标的名义 `N = 标的 ask × qty`（`notional_basis` 字段写明）。`ref_rate_apr` 默认 **0**（不预设任何贴现 / 机会成本收益）；给了才贴现并计 `capital_opp`。

**净边与门限**

```
net_edge_bps = gross_edge_bps − costs_bps.total
costs_bps    = fees + half_spread_slip + hedge_rebalance + borrow + transfer
             + capital_opp + funding_expected + funding_uncertainty + impact
safety_buffer_bps = fee_roundtrip_bps + slip_buffer_bps + funding_uncert_bps + model_haircut_bps
passes_threshold  = net_edge_bps > safety_buffer_bps ∧ persistence.ok ∧ liquidity.ok ∧ 无失效 flag
```

- 费率是 taker 占位值（spot 10 bp、perp 5 bp、期权 3 bp 名义且 ≤ 12.5% 权利金、结算 2 bp），全部 `--fee-*` 可改。A1 按往返 2 次计；A2 / A3 持有到期只计入场 + 结算。
- `half_spread_slip`：A1 记平仓时两腿半点差（入场点差已含在 bid/ask 腿里）；A2 / A3 为 0（到期结算），perp 锚除外。`impact`：按 `qty` 吃簿的 VWAP 相对盘口一档的滑点。
- `safety_buffer` 的 `fee_roundtrip_bps` 默认取**该记录自己算出的 fees**（不会低估），其余三项 `--buffer-*` 可调；输出里 `calibrated=false`，除非用 `--buffer-calibrated` 明示已按历史盘口标定。**分量之和，不是拍脑袋的「有边」数字。** 这个口径按提案是保守的（费用在成本与 buffer 里各算一次）。
- `persistence`：同一机会 key 连续 `--persistence-min-samples` 个快照 `net > buffer` 且跨度 ≥ `--persistence-min-sec`；一旦跌破就归零。单快照永远 `ok=false`（除非把 min-samples 设 1）。
- `liquidity`：每腿 `top_n` 档深度 ≥ `qty × depth_mult`，且能完整吃到 `qty`；否则 `illiquid` / `insufficient_depth`；期权单边无报价 → `one_sided_book`（缺的那一侧不会拿 mark 去猜，直接跳过该方向）。
- `edge_exceeds_buffer` 单独输出，方便看「原始信号」与「过滤后」的差别。
- `cost_engine` 块（§10，默认开、`--no-cost-engine` 关）：同一组 quote 成本桶交给 `tools/cost_engine.py` 重算，`all_in_cost_bps` 必须等于 `costs_bps.total`（不等即拒绝建记录），并给出 A1 的 `breakeven_funding_rate`（每期、使净边 = 0）；A2 / A3 为 `None`（funding 不是论点）。`costs_bps` / `safety_buffer` / `passes_threshold` 的算法与数值**完全不变**。

**输出**：每条机会一行 JSON（提案 §4 schema 的强制字段 + `taxonomy_base`、`edge_exceeds_buffer`、`invalidated_by`、`safety_buffer.components`、`notional_quote`、家族专属块 `funding` / `pcp` / `box`、`invalidation` 名单、`cost_engine` 块）。`--summary-out` 另落 summary：各家族记录数 / 越过 buffer 数 / pass 数 / 净边中位数，H-A1 / H-A2 / H-A3 状态与 `falsify_if`，完整 config 与 policy。

```bash
uv run python strategies/paper_arb_scanner.py --help

# 离线：回放合成盘口 fixture（4 个快照、30s 间隔；2–4 号快照故意抬高 ATM call 以覆盖越过 buffer / 持续度路径）
uv run python strategies/paper_arb_scanner.py --source fixture --out /tmp/arb.jsonl \
  --summary-out /tmp/arb-summary.json --print-summary --quiet

# 只看越过 buffer 的，并附保守 paper fill（吃 bid/ask 的 VWAP + 每腿再扣 5 bp 名义的 slip haircut；order_sent 恒 false）
uv run python strategies/paper_arb_scanner.py --only-exceeding --paper-fills

# 用 perp 当 PCP 远期锚（taxonomy 降级为 identity_approx_with_perp_proxy，funding 进成本）
uv run python strategies/paper_arb_scanner.py --pcp-anchor perp --quiet --print-summary

# 只读 live 观察：OKX 公共盘口 GET（无密钥），ETH spot / perp / ETH-USD 最近到期 3 个行权价，5 个快照 × 20s
uv run python strategies/paper_arb_scanner.py --source okx-public \
  --spot ETH-USDT --perp ETH-USDT-SWAP --opt-family ETH-USD --n-strikes 3 --max-expiries 1 \
  --samples 5 --interval-sec 20 --out /tmp/arb-live.jsonl --summary-out /tmp/arb-live-summary.json
```

每个 `okx-public` 快照约 11 个 GET（instruments ×2 仅首轮、spot / perp 盘口、funding、期权盘口 2 × 行权价数 × 到期数）。期权价按 OKX 惯例视作**币本位权利金**（每 1 币标的），簿 size 按 `ctVal` 折成币；结算锚（币 / USD 指数）与 USDT 现货不一致会打 `settlement_anchor_mismatch_vs_spot_quote_ccy`。

**验证分级（不得跳级）**：① `--source fixture` 历史 / fixture 回放 → ② `--source okx-public` live 只读观察 → ③ `--paper-fills` 纸面成交 → ④ 只有 ①–③ 通过才提交 `04-risk` + 用户确认。**本模块止步于 ③；不含、也不会含 live execution。** ChatGPT 分享页只是 idea 来源（`source_inspiration=chatgpt_share_unverified`），不是回测证据。fixture 里的越过 buffer 记录是合成的，不代表市场上存在机会。

### 9. 只读纸面对冲组合评分 · Phase B（`strategies/paper_combo_scanner.py`）

**研究侧支，与 §8 的 A1 / A2 / A3 并列，不替换它们，不并入现货网格 / `local_paper` 主线。** 对应提案 `paper-combo-strategies-phase-b-v1`（`03-proposals/2026-09-17-paper-combo-strategies-phase-b-v1.md` + 旁路 JSON）与 `04-risk/2026-09-17-…` **附条件通过（仅只读指标 + 纸面成交）**。Phase A 首扫 65 条净边全负是这条侧支的出发点：身份边薄 → 研究转向**显式相对价值组合**，仍是纸面；**不是**用杠杆或下单去「找回边」。**合 PR ≠ 放行交易；live execution 不在批准范围。**

代码层硬门禁（都有单测；实现复用 §8 的 `Book` / `Leg` / `Snapshot` / fixture 加载 / 流动性与持续度过滤 / OKX 公共只读源，不复制一份）：

| 门禁 | 实现 |
|------|------|
| `action` 恒 `observe_only`，`will_send_http` 恒 `false`，`phase` 恒 `"B"` | 常量 + `finalize_combo_record()`；改了就抛 `ObserveOnlyViolation` / `ComboSchemaViolation` |
| **全部 `taxonomy = relative_value`**；禁止无风险标签 | `taxonomy` 只接受 `relative_value`；记录 JSON 文本里出现 `risk_free` / `riskfree` / `无风险` / `稳赚` / `guaranteed` 任一字样 → `ForbiddenLabelViolation`；每条必带 `relative_value_not_riskless` |
| 继承 Phase A schema + `combo_id` / `hedge_mode` / `residual_risks` | `REQUIRED_FIELDS = Phase A 字段 + 4 个组合字段`；`combo_id` 必须与 `family` 对应（B1/B2/B3）；`hedge_mode` 白名单：B1 `static_combo`、B2 `paper_delta_sim_only`、B3 `options_rr_static` / `options_rr_plus_paper_delta`；`residual_risks` 必须**包含**该家族的强制名单（缺项拒绝） |
| 可执行价只能 bid / ask | 复用 Phase A `Leg` 闸门。**IV / delta / 模型公平价全部来自 mid，仅作选约与参照**（`model_fair_from_mid_iv_reference_only` flag；字段名带 `_ref`），永不作可执行价 |
| **不裸卖期权**（1x 概念） | `assert_covered_short_options()`：短 call ↔ 多头标的（qty ≥）或同类多头期权且**到期 ≥**；短 put 同理。**B1 的短 call 必须由 spot 名义覆盖**（perp 多头不算）；**反向日历**（卖远买近）到期后裸奔 → 拒绝；**B3 无翼 RR 不建记录**（计入 `skipped.b3_no_wing_cover_*`），有翼才评分。`cover` 字段写明每条短腿被谁覆盖 |
| **B2 delta 对冲仅纸面模拟** | `paper_delta_hedge_sim()` 纯算术，无任何 HTTP；输出块 `paper_delta_sim` 恒 `live_hedge_http=false` / `order_sent=false`；B2 记录必须 `enabled=true`，其成本进 `costs_bps.hedge_rebalance`（不许藏进「残差」） |
| 零 Trade / amend / 提现 / 划转 | 唯一网络路径仍是 `tools/okx_readonly_client.py` 公共 GET；单测断言源码不含 `/api/v5/trade`、`urlopen`、`POST`，且不 import 网格模块 |
| `safety_buffer` 含 `vol_path_haircut` 且默认未标定 | `ComboSafetyBuffer = fee_roundtrip + slip + funding_uncert + vol_path_haircut + model_haircut`，`calibrated=false`，输出里明写「未标定不得喊有可交易边」 |

**三个家族（同所，默认 OKX · ETH 概念）**

| ID | family | hedge_mode | 腿（买 @ask / 卖 @bid） | 毛边（quote，除以 `N = spot ask × qty` 得 bps） | 强制残留风险 |
|----|--------|-----------|--------------------------|-----------------------------------------------|--------------|
| B1 | `B1_covered_call_carry` | `static_combo` | 买 spot @ask + 卖 perp @bid（名义对齐）+ 卖 OTM call @bid（Δ ∈ `[0.15, 0.25]`，无匹配则退到 moneyness 带 `[5%, 30%]` 并打 `otm_selection_by_moneyness_fallback`；每到期最多 2 条） | `min(f_now, f_next) × H × N` + `(call_bid − fair_ATMσ)` + 基差（不利必记、有利 opt-in）。`fair_ATMσ` = 同到期 ATM mid IV 的 Black-76 公平价（平坦偏斜参照）；即「卖的 call 相对 ATM 有多富」。theta 不记（`theta_carry_credited=false`）。负 funding → 记 `funding_expected` 成本；预测翻号 → `funding_sign_flip_predicted` 失效 | `gamma` `funding_flip` `gap` `margin` `basis` `capped_upside` |
| B2 | `B2_calendar_vol` | `paper_delta_sim_only` | 买远月 @ask + 卖近月 @bid，**同类型同 K**（默认 call，`--b2-opt-type P` 对照）；相邻到期配对，ATM 附近最多 6 个 K | `fair_debit(σ_near) − exec_debit`，`exec_debit = far_ask − near_bid`，`fair_debit = B76(far, σ_near_mid) − B76(near, σ_near_mid)`（平坦期限参照 → 远月相对近月 vol 便宜才有边）。纸面 delta 对冲：入场对冲 `−Δ_net`、每日 3 次再平衡各交易 `E|Δδ| = |Γ|·S·σ·√dt·√(2/π)`，每笔付费 + 半点差 + 2 bp slip；perp 对冲付侧 funding 记成本、收侧不记；全部进 `hedge_rebalance` / `funding_*` | `gamma` `vega_term_structure` `gap` `margin` `hedge_slippage_underestimation` `model_risk` |
| B3 | `B3_put_skew_rr` | `options_rr_static`（`--b3-paper-delta` → `options_rr_plus_paper_delta`） | 同到期：25Δ call 与 25Δ put（mid IV 求 Δ，最近者），**加更远 OTM 10Δ 同类多头翼覆盖短腿**。`long_rr`：买 call @ask + 卖 put @bid + 买 put 翼 @ask；`short_rr`：卖 call @bid + 买 put @ask + 买 call 翼 @ask | `RR = IV(call) − IV(put)`；`RR_exec` 用买腿 ask / 卖腿 bid 的 IV 合成；参照 `RR_ref` 默认 = **之前快照** mid RR 的滚动中位数（≥ 2 个先验样本，否则 `rr_reference_unavailable` 失效，边按自身 mid 算只剩点差成本），或 `--b3-rr-ref-mode fixed`。边 = 有符号偏离（vol 点）× 平均 vega。翼视为公平价保险：只记其半点差 + 费用，不记边 | `skew_trend` `gamma` `gap` `margin` `spot_direction_bleed` `liquidity` |

`costs_bps` = Phase A 九项 + `vol_path_haircut`（每条短期权腿默认 10 bp 名义；`--vol-path-haircut-bps`）。持有期统一按 `--horizon-intervals`（默认 3 × 8h = 1 天）：到期 ≤ 持有期 → 结算费；否则按**再跨一次全价差**回购（`hold.exit_rule`）。`margin_capital_required`：B1 = spot 名义 + perp 1x 保证金（call 由 spot 备兑，不另计）；B2 = 净支出 + 对冲名义；B3 = 支付权利金 + 翼距（定义最大亏损）+（若开）对冲名义。

**H-B1 对照 A1**：同一快照上顺手跑 §8 的 A1（复用 `ArbScanner.scan_a1`），每条 B1 记录带 `a1_reference_same_window` 与 `b1_minus_a1_net_edge_bps`，summary 里 `hypotheses.H-B1.a1_pass_rate_same_window` / `b1_pass_rate_minus_a1`。平坦偏斜下备兑边 = −点差 → B1 天然劣于 A1；只有 call 相对 ATM 明显偏富时才反过来。A1 对照**不是** Phase B 记录，不进 JSONL。

`cost_engine` 块（§10，默认开、`--no-cost-engine` 关，A1 对照臂同一开关）：十项 quote 成本桶（含 `vol_path_haircut`）交给 `tools/cost_engine.py` 重算，`all_in_cost_bps == costs_bps.total` 否则拒绝；B1 给 `breakeven_funding_rate`（负 funding 时 `funding_expected` 记成本，breakeven 公式自动扣回），B2 / B3 为 `None`（vol 论点，funding 只经纸面对冲进成本）。`finalize_combo_record()` 额外要求该块 `observe_only`、`tradable_claim_allowed=false`、含 `vol_path_haircut`。

**输出**：每条一行 JSON（提案 §6 schema 全部强制字段 + `cover`、`paper_delta_sim`、`hold`、家族块 `call` / `term_structure` + `calendar` / `rr` + `strikes` + `deltas_mid_ref`、`invalidation` 名单、`related_phase_A`）。summary：各家族记录 / 越过 buffer / pass / 净边中位数 / `hedge_modes`，H-B1 / H-B2 / H-B3 状态与 `falsify_if`，`a1_reference_same_window`，`skipped` 计数（如 `b2_single_expiry`、`b3_no_wing_cover_long_rr`），`live_delta_hedge=false`，完整 config 与 policy。

```bash
uv run python strategies/paper_combo_scanner.py --help

# 离线：回放双到期合成 fixture（4 快照 × 30s；2–4 号快照故意压低远月 vol、抬高近月 2800 call，3–4 号抬高近月 2200 put，
# 以覆盖 B2 / B1 越过 buffer → 持续度 → pass 与 B3 越过但不 pass 的代码路径；全部合成，非行情）
uv run python strategies/paper_combo_scanner.py --source fixture --out /tmp/combo.jsonl \
  --summary-out /tmp/combo-summary.json --print-summary --quiet

# 只看越过 buffer 的，附保守 paper fill；B3 用备选「RR + 纸面 delta」模式
uv run python strategies/paper_combo_scanner.py --only-exceeding --paper-fills --b3-paper-delta

# Phase A fixture 也能加载（单到期 → B2 无配对、B3 无翼 → 零记录，summary.skipped 说明原因）
uv run python strategies/paper_combo_scanner.py --fixture fixtures/arb_books/2026-09-16-eth-books-sample.json --print-summary --quiet

# 只读 live 观察：OKX 公共盘口 GET（无密钥），最近 2 个到期 × 8 个行权价，5 个快照 × 20s
uv run python strategies/paper_combo_scanner.py --source okx-public \
  --opt-family ETH-USD --n-strikes 8 --max-expiries 2 --samples 5 --interval-sec 20 \
  --out /tmp/combo-live.jsonl --summary-out /tmp/combo-live-summary.json
```

**分阶段（不得跳级，同提案 §7）**：① 只读指标（本模块 `--source fixture` / `--source okx-public`）→ ② 纸面成交（`--paper-fills`，B2 纸面 delta 路径）→ ③ 风控审查 → ④ **用户确认**后才允许讨论日后执行请求。**本模块止步于 ②。** 明确不做：裸卖波动率（默认否决）、跨所原子成交、实盘动态 delta、用 mark / mid 报边、并入网格主线、>1x。B3 的 put skew 垂直价差备选扫描未实现（本版只做带翼 RR 主扫描）。

### 10. Cost Engine v0 · 全成本 / 净边 / breakeven funding（`tools/cost_engine.py`）

**研究侧支，Phase C 的 C0 / 路线图 P0**（`03-proposals/2026-09-17-impl-roadmap-from-share-v1.md` §4 P0；`04-risk` 同日**附条件通过：仅 Phase C 纸面 / 只读工程，Execution 拒绝**）。做的事只有一件：把 §8 / §9 记录里各自内联的 `costs_bps` 算术升格为**一个可复用、可单测的模块**，并补上「使净边 = 0 的 funding 门槛」。**不改**现货网格 / `local_paper` 主线（不 import 网格模块，也不被网格模块 import）；**不含** Fair-value / basis（C1）、Funding expectation（C3）、Opportunity Score schema、Radar 字段、任何 Execution。**合 PR ≠ 放行交易。**

代码层硬门禁（都有单测 + fixture 拒绝用例）：

| 门禁 | 实现 |
|------|------|
| `action` 恒 `observe_only`，`will_send_http` 恒 `false` | 每个结果 / 报告都带；`finalize()` 校验，改了就抛 `ObserveOnlyViolation`。模块**没有任何网络代码**（单测断言源码无 `urllib` / `socket` / trade 路径） |
| 可执行价只能 bid / ask | `LegSpec`：买腿必须 `ask`、卖腿必须 `bid`；`mark` / `mid` / `last` → `ExecutablePriceViolation`；mark 只能作 `mark_ref`；**单边簿直接拒绝，不拿 mark 去猜** |
| `calibrated=false` 为默认，不得升级「可交易」话术 | `CostEngineConfig(calibrated=True)` 必须带 `calibration_ref`（04-risk 标定笔记），否则 `CalibrationClaimRefused`；即使标定了，输出仍恒 `tradable_claim_allowed=false`。**v0 里 A/B 接线一律 `calibrated=false`** |
| **禁止 `current_funding × 365` 当净边** | `evaluate()` 只接受持有期毛边（`gross_basis="hold_horizon"`），传 `annualized` / `apy` / `x365` → `NaiveAnnualizationRefused`；`FundingLeg` 期望 = `min(\|now\|, \|next\|) × H` 期，期数超一年也拒绝；输出恒 `annualized=false` / `edge_basis="hold_horizon"`；年化只以 `_ref` 展示字段出现（`apy_ref()` / `breakeven_funding.apr_ref`，带 `display_only=true` / `return_promise=false`） |
| 无收益承诺 | 输出文本含 `risk_free` / `无风险` / `稳赚` / `guaranteed` → 拒绝；`disclaimer` 明写「门槛不是预测」 |

**八个分量**（全部先算 quote 币金额，再除以标的名义 `N` 得 bps；每项四位小数后求和，与 A/B 记录**同一口径**，因此 `all_in_cost_bps` 必须逐条等于 `costs_bps.total`）：

| 分量 | 原语 | 口径 |
|------|------|------|
| `fees` | `fee_quote()` | spot / perp：名义 × taker × crossings；期权：每次 crossing `min(标的名义 × 3 bp, 权利金 × 12.5%)`，持有到期另加 2 bp 结算费 |
| `half_spread_slip` | `half_spread_quote()` | `(ask − bid)/2 × qty`，**只记额外 crossings**（入场点差已在 bid/ask 可执行价里）；单边簿拒绝 |
| `impact` | `impact_quote()` | `\|VWAP − 盘口一档\| × qty`（吃簿冲击；无 VWAP 记 0） |
| `borrow` | `borrow_quote()` | 借币名义 × APR × 持有年 |
| `transfer` | `transfer_quote()` | 名义 × bps |
| `capital_opp` | `capital_opp_quote()` | 占用资金 × `ref_rate_apr` × 持有年（`ref_rate_apr=0` = 不预设机会成本） |
| `funding_uncertainty` | `funding_uncertainty_quote()` | `N × (σ/期 × H + \|now − next\| × H)` |
| `hedge_rebalance` | `hedge_rebalance_quote()` 或直接传入纸面 delta 模拟的 quote 成本 | 再平衡允当 |

A/B 已有的 `funding_expected`（funding 记成本的方向）与 `vol_path_haircut`（Phase B）作为**已知附加项**一并求和、原样透传（`components_extra`）；未知键 / 缺核心键 / 负值只打 `flags`，不静默丢弃。`LegSpec` + `leg_costs()` / `evaluate_legs()` 可直接从 bid/ask 腿出前三项（币本位权利金用 `quote_conv` 折算），与手填桶的 `evaluate()` 二选一，重复传同名桶会拒绝（防双计）。

**输出**（`CostResult.to_dict()`）：`all_in_cost_bps`、`gross_edge_bps`、`net_edge_bps = gross − all_in`（持有期口径）、`components_bps`（八核心 + 附加）、`components_quote`、`breakeven_funding_rate`（每期小数）与 `breakeven_funding` 块、`calibrated` / `calibration_ref`、`tradable_claim_allowed=false`、`annualized=false`、`flags`、`disclaimer`。

**breakeven funding**（`FundingContext(intervals=H, funding_gross_quote=G_f, funding_cost_quote=C_f)`）：记 `G_o` / `C_o` 为除 funding 以外的毛边 / 成本，则 `net = (G_f − C_f) + G_o − C_o`，而 `(G_f − C_f) = f × H × N`，于是

```
breakeven_funding_rate  f* = (C_o − G_o) / (H × N)          # 每期，持仓收到方向
headroom_bps_per_interval  = (f_implied − f*) × 1e4       # f_implied = (G_f − C_f)/(H×N)
net_edge_bps               ≈ headroom × H                  # 恒等式，单测 + CI 断言
```

`funding_uncertainty` 留在 `C_o`（不随 f 变），因此 f* 是**门槛，不是预测**。无 funding 论点（A2 / A3 / B2 / B3）→ `rate_per_interval: null` + `reason: no_funding_context`；`H = 0` → `zero_horizon_intervals`。`apr_ref = f* × 每年期数` 仅展示。默认 fixture 里 A1（3.0 → 2.5 bp/8h，3 期）的 breakeven ≈ **15.7 bp/期**，实际用 2.5 bp → headroom −13.2 bp/期 × 3 = 净边 −39.5 bp：与 §8「funding 打不过 30 bp 往返手续费」同一结论，只是现在给了阈值。

```bash
uv run python tools/cost_engine.py --help

# 离线回放 fixture 用例（17 条：手算数字 + 7 条拒绝用例），不匹配退出码 1；--full 带每条完整结果
uv run python tools/cost_engine.py --cases fixtures/cost_engine/2026-09-17-cost-cases.json --full --out /tmp/cost-cases.json

# 在 A/B 扫描器里：默认每条记录带 cost_engine 块，summary 带 cost_engine 聚合（中位 all_in / breakeven）
uv run python strategies/paper_arb_scanner.py --print-summary --quiet
uv run python strategies/paper_combo_scanner.py --no-cost-engine --quiet --print-summary   # 关掉该块
```

Python 里：

```python
import cost_engine as ce   # tools/ 在 sys.path 上

res = ce.evaluate_legs(
    legs=[ce.LegSpec("ETH-USDT", "spot", "buy", 1.0, bid=1999.0, ask=2001.0, vwap=2001.0),
          ce.LegSpec("ETH-USDT-SWAP", "perp", "sell", 1.0, bid=2002.0, ask=2003.0)],
    fees=ce.FeeSchedule(), gross_quote=1.50075, underlying_notional_quote=2001.0,
    other_components_quote={"hedge_rebalance": 0.4002, "funding_uncertainty": 1.50075},
    funding=ce.FundingLeg(rate_now=0.0003, rate_next=0.00025, horizon_intervals=3).context(2001.0),
)
res.all_in_cost_bps, res.net_edge_bps, res.breakeven_funding_rate   # 47.0013, -39.5013, ≈0.001567/期
```

**验收（路线图 P0）**：单测固定 fixtures ✔；未标定不得 `passes_threshold` 话术升级 ✔（引擎不产生 `passes_threshold`，A/B 的门限逻辑原样保留，且块内恒 `tradable_claim_allowed=false`）。**下一拍**（均须新的 `04-risk`）：P1 Fair-value / basis、P2 Funding expectation（与本模块 breakeven 对照）、P3 Opportunity Score schema、P4 Radar `_ref` 字段。

`LegSpec` 自 T1 起多一个**可选** `slip_crossings`（默认 `None` = 沿用 `crossings − 1`，A/B 数值不变）：把「记多少次额外半点差」与「收多少次手续费」解耦。单程多腿现货环用 `crossings=1`（每腿一次手续费）+ `slip_crossings=1`（每腿再记一次半点差，作非原子重报价 haircut）。

### 11. 只读纸面现货三角扫描器 · 菜单 T1（`strategies/paper_triangle_scanner.py`）

**研究侧支，纸面 / 只读，不替换现货网格 / `local_paper` 主线。** 对应 `03-proposals/2026-09-17-demo-strategy-menu-stable-edge-v1.md` §2.1 T1 与 `04-risk/2026-09-17-demo-strategy-menu-stable-edge-v1.md` 的 **附条件放行：D0–D3 只读扫描 + paper fills；本批不授权 demo 下单；live 否决**。**合 PR ≠ 放行任何下单。** 本节不含、也不会含「三角稳定利润」结论：菜单 §0.3 的已知事实是 A+B 95 条记录 `net>0 = 0%`，T1 的假设 `H-T1` 在净边未复现前**视为未证伪**，扫描器的任务就是给出可证伪的数字。

代码层硬门禁（都有单测；fixture 覆盖每条路径）：

| 门禁（04-risk 标定） | 实现 |
|----|------|
| `action` 恒 `observe_only`，`will_send_http` 恒 `false` | 模块常量 + `finalize_record()`；改了就抛 `ObserveOnlyViolation`。**无** Trade / amend / withdraw / transfer 路径；唯一网络路径是 `tools/okx_readonly_client.py` 的公共 `GET /market/books`（`--source okx-public`，默认 fixture 离线）。单测断言源码不含 `/api/v5/trade`、`urlopen`、`POST`、`place_order(`、`amend_algo(`、`withdraw(`、`cancel_order(`，且不 import 网格模块 |
| **白名单**：`ETH-USDT` · `BTC-USDT` · `ETH-BTC`（双向环） | `Triangle.validate()`：其他三元组 → `TriangleNotWhitelisted`（CLI 退出码 1）；扩其他三元组须点名另批 |
| taxonomy | 恒 `same_venue_microstructure`；`risk_free` / `无风险` / `稳赚` / `guaranteed` 出现在任何字段 → `ForbiddenLabelViolation`；每条记录带 `same_venue_microstructure_not_riskless` + `non_atomic_three_leg` flag |
| 可执行价只能 bid / ask | 买腿 `ask`、卖腿 `bid`（`CycleLeg.__post_init__` + `finalize_record`）；mid / mark / last 拒绝；**单边簿跳过该方向不猜**（`summary.skipped.one_sided_book`） |
| **同步**：三簿共窗时间戳 | `book_sync`：`skew = max(ts) − min(ts)`；`> --max-book-skew-ms`（默认 **200**）→ `stale_book`；缺时间戳 → `book_timestamp_missing`；两者都是失效 flag，**记录照常落盘但永不 `passes_threshold`**，且 `synthetic_fill.ok=false`。每条记录带 `book_sync.sync_gate="strict"`——模块里**只有** strict 一种门；若日后 04-risk 书面放宽，必须以 `sync_gate=relaxed` + 分表出现，禁止静默改阈值（单测断言源码无 relaxed 模式）。降 skew 靠**拉取方式**（下文「同步修复」），不靠改门 |
| **流动性**：每腿半点差 ≤ 15 bp | `half_spread_bps = (ask − bid)/2 / bid`（宽度度量，不是价）；`> --max-half-spread-bps`（默认 **15**）→ `half_spread_over_cap` + `illiquid`；另沿用 Phase A 深度门（`top_n` 档 ≥ `qty × depth_mult`、能完整吃到 `qty`，否则 `insufficient_depth`） |
| **成本 ≥ fees + half_spread/slip + impact，且净边只经 Cost Engine** | 三条 `ce.LegSpec`（spot，`crossings=1`，`slip_crossings=--nonatomic-slip-half-spreads`，VWAP 来自吃簿）→ **`ce.evaluate_legs()`**；记录的 `gross_edge_bps` / `net_edge_bps` / `costs_bps.*` / `costs_bps.total` **逐字取自** `cost_engine` 块（`net_edge_source="tools/cost_engine.py:evaluate_legs"`）；`finalize_record` 校验三者与块一致（1e-9），不一致拒绝。模块**没有**自己的净边算术。单测用 spy 断言 `evaluate_legs` 真被调用 |
| `residual_risks` 必填 | 恒含 `non_atomic_three_leg`、`leftover_inventory_on_partial_or_cancelled_leg`、`impact_and_queue_position`、`fee_tier_uncertainty`、`book_staleness_between_legs`、`demo_vs_live_liquidity_gap`；缺任何一项拒绝 |
| 杠杆概念 1x | `leverage_concept=1`；`margin_capital_required = home 名义`（现货全款） |

**环与算术**（home = USDT，默认 `--notional-quote 100` USDT 推一圈）

| direction | 路径 | 腿（买 @ask / 卖 @bid） | 毛边 > 0 条件 |
|-----------|------|--------------------------|--------------|
| `usdt_eth_btc_usdt` | USDT → ETH → BTC → USDT | 买 ETH-USDT @ask → 卖 ETH-BTC @bid → 卖 BTC-USDT @bid | `bid(ETH-BTC) > ask(ETH-USDT) / bid(BTC-USDT)` |
| `usdt_btc_eth_usdt` | USDT → BTC → ETH → USDT | 买 BTC-USDT @ask → 买 ETH-BTC @ask → 卖 ETH-USDT @bid | `ask(ETH-BTC) < bid(ETH-USDT) / ask(BTC-USDT)` |

```
gross_quote  = home_out − home_in                       # 三腿 bid/ask 串乘，未扣成本
gross / net / costs  ←  cost_engine.evaluate_legs(...)   # 唯一来源
  fees              = Σ 腿可执行名义 × spot_taker（10 bp 占位）× 1 crossing
  half_spread_slip  = Σ (ask−bid)/2 × qty × slip_crossings（默认 1：非原子重报价 haircut）
  impact            = Σ |VWAP − 一档| × qty（按 qty 吃簿）
  borrow / transfer / funding_uncertainty / hedge_rebalance = 0；capital_opp = N × ref_rate_apr × 0（环在秒级，hold_years=0）
safety_buffer_bps = fee_roundtrip(记录自己的 fees) + slip 5 + nonatomic 5 + model 5   # 分量之和，calibrated=false
passes_threshold  = net > buffer ∧ persistence.ok ∧ liquidity.ok ∧ book_sync.ok ∧ 无失效 flag
```

`ETH-BTC` 腿以 BTC 计价，成本折算到 USDT 一律用 **BTC-USDT ask**（`quote_conv_to_home`，偏保守）。`bps` 相对 `home_in`（`notional_basis="home_ccy_notional_in"`）。

**输出**（每条一行 JSON）：`direction` / `triangle`（路径、`home_in` / `home_out_before_costs`、`cross_exec` vs `cross_implied_from_directs`、`cross_favourable_deviation_bps`）、三腿 `legs`（qty、可执行价、`price_type`、计价币、`quote_conv_to_home`、簿时间戳）、`executable_prices`、`book_sync`（每簿 ts、`skew_ms`、`ok`）、`liquidity`（每腿 `half_spread_bps` / 深度 / VWAP / `complete`）、`gross_edge_bps` / `costs_bps` / `net_edge_bps` / `dominant_cost_component`、`cost_engine` 块、`safety_buffer`、`gross_positive` / `net_positive` / `cost_killed` / `edge_exceeds_buffer` / `passes_threshold` / `invalidated_by`、`synthetic_fill`（三腿在「同时可成交」假设下能否全部吃完；`order_sent` 恒 false）、`residual_risks`、`risk_flags`、`hypothesis_id=H-T1`。

**summary**（`--summary-out`）：`metrics.net_positive_rate`（`net_edge_bps > 0` 占比）、`cost_kill_rate`（`gross > 0 ∧ net ≤ 0` 在 `gross > 0` 中占比）、`synthetic_fill_success_rate`、`stale_book` / `illiquid` 计数、越过 buffer / pass 数、净边中位 / 最大、`dominant_cost_component` 直方图；按 `directions` 分拆；`H-T1` 状态只有 `no_samples` / `insufficient_samples_for_persistence` / `no_pass_on_window` / `not_yet_falsified_on_window`；`cost_engine` 聚合；完整 config / policy / `mainline_unchanged`。

```bash
uv run python strategies/paper_triangle_scanner.py --help

# 离线：回放合成三腿 fixture（8 快照 × 30s：无边 → 交叉盘 +20 bp 被成本杀 → +100 bp 越过 buffer 并在第 3 次持续后 pass
# → 交叉簿滞后 350 ms 标 stale_book → 半点差 27 bp 标 illiquid → 交叉 bid 深度不足合成成交失败；全部合成，非行情）
uv run python strategies/paper_triangle_scanner.py --source fixture --out /tmp/t1.jsonl \
  --summary-out /tmp/t1-summary.json --print-summary --quiet

# 只看越过 buffer 的；把 venue 标成 okx_demo（仅标签，公共簿同源）
uv run python strategies/paper_triangle_scanner.py --only-exceeding --venue okx_demo

# 只读 live 观察：OKX 公共盘口 GET（无密钥），三腿各 1 个 GET / 快照，10 个快照 × 20s
uv run python strategies/paper_triangle_scanner.py --source okx-public --samples 10 --interval-sec 20 \
  --out /tmp/t1-live.jsonl --summary-out /tmp/t1-live-summary.json

# 收紧 / 放宽门（都会写进 summary.config）；扩白名单外三元组会被拒绝
uv run python strategies/paper_triangle_scanner.py --max-book-skew-ms 100 --max-half-spread-bps 10 --nonatomic-slip-half-spreads 2
uv run python strategies/paper_triangle_scanner.py --leg-a SOL   # → error: … not on the 04-risk whitelist
```

`okx-public` 每快照 3 个 GET（三腿现货簿，size 即币数）；偏差按 OKX 返回的簿时间戳算并过 200 ms 门——**live 只读观察里 `stale_book` 出现是门禁在工作，不是 bug**。

**同步修复 · T1 stale-book fix v1（`03-proposals/2026-09-18-t1-stale-fix-reread-v1.md` + `04-risk` 同名附条件通过；纸面 / 只读，零 demo 下单）**

T1-001 / T1-002 共 48 条记录 **`stale_book=48/48`**、skew ≈ 399–499 ms，同步合格子集为空，「边不存在」与「门把样本全剔」无法区分。根因（假说 H1）：三簿**顺序** GET，第 1 簿与第 3 簿的场馆时间戳相隔两个完整往返。按 04-risk 优先序，本修复只动**拉取方式**，不动门：

| 项 | 实现 | 默认 |
|----|------|------|
| (1) 并行拉取 | `--fetch-mode parallel`：三腿 GET 用 3 线程同时发出，skew 只剩服务端时钟差 + 线程启动抖动，不再累加往返；`sequential` 保留为 001/002 口径的对照臂 | `parallel` |
| (2) 同窗对齐 | `--sync-retries N`：若并行后 skew 仍 > 门，**同一快照内**再拉最多 N 次，取第一次落入门内的一组；全部失败则保留最后一组，**照旧标 `stale_book`**。每次尝试的 skew 写进 `book_sync.fetch.attempt_skews_ms`，被弃的簿不打分 | `0`（关） |
| (3) 放宽门 | **未实现**。`--max-book-skew-ms` 默认仍 200；`sync_gate` 恒 `strict` | — |
| 时间戳留痕 | `book_sync.fetch`：`mode` / `attempts` / `selected_attempt` / 每腿 `sent_ms` `recv_ms` `latency_ms` `book_ts_ms`（本地钟 vs 场馆钟分列）/ `fetch_span_ms` / `send_spread_ms` / `book_ts_skew_ms`；fixture 快照该字段为 `null` | 总是写 |
| 分表报告 | summary `sync_report`：`stale_book_rate` / `synced_rate`、skew 分布（min / median / p90 / max / 门内占比）、`subsets.all` vs `subsets.synced`（`!stale_book`）vs `subsets.stale_or_missing` 各自的 `net_positive_rate` / `cost_kill_rate` / `pass_rate`；`hypotheses.H-T1.synced_subset` 同步给出子集口径 | 总是写 |

单测里的伪 OKX（多线程、每请求 120 ms 延迟、`ts` = 到达时刻）给出 before / after：顺序拉取 skew ≥ 240 ms（确定性下界，> 200 → `stale_book`），并行拉取 skew 落在门内；发送间隔从 ≥ 240 ms 降到个位数 ms，每快照耗时从 ≈ 3 个延迟降到 ≈ 1 个。**这只说明同步可度量，不说明有边。**

```bash
# 只读重扫（提案 §3 步 C；与 002 可比：≥18 快照、双向、interval 20s；纸面，不下单）
uv run python strategies/paper_triangle_scanner.py --source okx-public --venue okx_demo \
  --samples 18 --interval-sec 20 --fetch-mode parallel \
  --out /tmp/t1-003.jsonl --summary-out /tmp/t1-003-summary.json --print-summary --quiet

# 同窗对齐（可选）：并行后仍出门则同快照内再拉最多 2 次；每次 skew 都留痕
uv run python strategies/paper_triangle_scanner.py --source okx-public --samples 18 --interval-sec 20 \
  --fetch-mode parallel --sync-retries 2 --out /tmp/t1-003r.jsonl --summary-out /tmp/t1-003r-summary.json

# 对照臂：001/002 的顺序拉取口径（预期 stale 率仍高）
uv run python strategies/paper_triangle_scanner.py --source okx-public --samples 18 --interval-sec 20 \
  --fetch-mode sequential --summary-out /tmp/t1-003-seq-summary.json --quiet

# 读分表：全样本 vs 同步合格子集
python3 -c "import json; s=json.load(open('/tmp/t1-003-summary.json'))['sync_report']; \
  print(s['stale_book_rate'], s['synced_rate'], s['skew_ms']); \
  print({k: (v['records'], v['net_positive_rate'], v['cost_kill_rate'], v['pass_rate']) for k, v in s['subsets'].items()})"
```

重扫结论口径（同 04-risk）：`synced_rate > 0`（理想 ≥ 0.2）= **方法成功**；`!stale` 子集上 `net>0` 可复现（≥ 2 独立窗）才允许**另案起草** demo 小仓（本模块不授权）；stale 降了但子集上 `net>0 ≈ 0` 且 cost_kill 高 = **证伪 H1-边**，停止 chase。产出写 `02-metrics/sim/`。

**分阶段（不得跳级，同 04-risk D0–D3）**：① `--source fixture` 回放 → ② `--source okx-public` 只读观察 → ③ 汇总 `net_positive_rate` / `cost_kill_rate` / 合成成交率 → ④ 若净边可复现，另写 demo 小仓规格并经 **04-risk 再批 + 用户明确确认**。**本模块止步于 ③。** 明确不做：demo / live 下单、IOC、跨所延迟套利、用 mid / mark 报边、白名单外三元组、>1x、并入网格主线、任何收益 / APY / 「稳赚」话术。fixture 里越过 buffer 的记录是合成的，不代表市场上存在机会。

### 12. 只读纸面组合扩搜 · C1–C4 低腿 / 深簿 / 可证伪（`strategies/paper_combo_expansion.py`）

**研究侧支，与 §8 A1–A3、§9 B1–B3、§11 T1 并列，只扩不替换，不并入现货网格 / `local_paper` 主线。** 对应提案 `combo-expansion-low-leg-v1`（`03-proposals/2026-09-17-combo-expansion-low-leg-v1.md` + `.json`）与 `04-risk/2026-09-17-combo-expansion-low-leg-v1.md` **附条件通过（仅只读指标 + 纸面 / observe_only 扩搜）**。触发事实：Cost Engine v0 复算 A+B 共 95 条，gross>0 ≈ 22%，**net>0 = 0%**（A 族死于 fees，B 族死于 spread+slip+fees+vol_path_haircut，且 B1/B3 本窗 n=0）。结论不是「加腿找回边」，而是找**更少腿、更深簿、先有样本再谈成本**的纸面候选。**合 PR ≠ 放行交易；本模块不含也不会含 live execution。**

代码层硬门禁（都有单测；`finalize_expansion_record()` 拒绝而不是记日志）：

| 门禁 | 实现 |
|------|------|
| `action` 恒 `observe_only`，`will_send_http` 恒 `false`，`phase` 恒 `"C"` | 常量 + finalize；改了抛 `ObserveOnlyViolation` / `ExpansionSchemaViolation`。零 Trade / amend / withdraw / transfer 代码；唯一网络路径仍是 §6 公共 GET（复用 §8 的快照源，另加 FUTURES 元数据 + 盘口 GET）；单测断言源码不含 `/api/v5/trade`、`urlopen`、`POST`，不 import 网格模块 |
| **净边只来自 `tools/cost_engine.py evaluate`** | C1/C2 记录的 `gross_edge_bps` / `costs_bps` / `net_edge_bps` **直接读取** `ce.evaluate()` 结果（模块内没有 `gross − cost` 的旁路算术，单测扫源码）；C3/C4 继承 A/B 记录并复核 `cost_engine.net_edge_bps == net_edge_bps`；`cost_engine` 块为**必填**字段；每条带 `net_edge_source="tools/cost_engine.py:evaluate"` |
| **全部 `taxonomy = relative_value`**；禁无风险标签 | C4 继承的 A2/A3 原 `identity_approx` 保留在 `taxonomy_base`，顶层一律 `relative_value`；记录文本含 `risk_free` / `无风险` / `稳赚` / `guaranteed` → `ForbiddenLabelViolation`；每条必带 `relative_value_not_riskless` |
| 可执行价只能 bid / ask | 复用 Phase A `Leg` 闸门；mid / mark / 模型公平价只以 `_ref` 出现（`fair_value.used_in_net_edge=false`、`basis_mid_ref_only=true`） |
| **stub 模型必须标注** | C1 fair-value、C2 funding 期望均为 `model="stub"` · `calibrated=false`；finalize 拒绝其它取值；`calibrated=true` → `CalibrationClaimRefused`（本版无标定路径） |
| **禁 `current_funding × 365` 进净边** | C2 毛边 = Σ_h f_h（H 期路径和）；`funding_model.annualized_used_in_net_edge=false` 否则 `NaiveAnnualizationRefused`；`ce.evaluate(gross_basis="hold_horizon")`；年化只以 `current_funding_annualized_ref`（`display_only` / `is_net_edge=false` / `banned_from_net_edge=true`）展示；源码不含 `* 365` / `* 1095` |
| **默认 ETH；BTC 须点名 flag** | `--underlying BTC` 无 `--allow-btc` → `UnderlyingNotApproved`（**本批 04-risk 未放行 BTC**，flag 默认关）；快照里出现非当前标的的合约同样拒绝 |
| 腿数 / 对冲备注 | C1、C2 **≤ 2 可交易腿**；远月 / perp 只进 `hedge_note`（`tradeable_default=false`，否则拒绝）；C4 禁止再叠「优化第四腿」（A2 ≤3、A3 ≤4） |
| **不裸卖 vol · 1x** | `assert_covered_short_options()` 在 finalize 再跑一遍；C3 的 B1 短 call 必须由 **spot 名义**覆盖（perp 多头不算）；`leverage_concept` 恒 1 |
| **流动性不过 ≠ pass** | `liquidity_ok=false` 的记录 `passes_threshold` 必为 false 且带 `c4_illiquid_leg`（否则拒绝） |
| **覆盖门 N≥5 才许下结论** | `coverage_verdict()`：n<N 只能写 `coverage_insufficient_no_verdict`；n≥N 且 net>0=0 → `cost_veto_on_window`；有 net>0 → `net_positive_observed_uncalibrated_not_tradable`。结论字串禁含 `falsified` / `dead` / `valid` / `已证伪` / `有效`（`BANNED_VERDICT_WORDS`，单测 + 运行时校验） |
| 风控标定在 config 层锁死 | `--c4-max-half-spread-bps` 只能 ≤25、`--c4-min-size-contracts` ≥1、`--c4-min-notional-quote` ≥50、`--coverage-n` ≥5、C3 只接受 `delta_moneyness_band_plus_5pp_symmetric` 且步长固定 0.05；放宽即 `ValueError` |

**四个扩搜族（同所，默认 OKX · ETH；`combo_id` 前缀 C1–C4）**

| ID | `combo_id` | 内容 | 腿（买 @ask / 卖 @bid） | 毛边（quote，除以 `N = spot ask × qty`） | 关键字段 / 失效 |
|----|-----------|------|--------------------------|------------------------------------------|-----------------|
| **C1** | `C1` | 现货 vs **交割合约**基差 / near–far 期限曲线（`H-C1`） | spot + **最近到期**交割合约（`--c1-tradeable-expiries`，默认 1）；`F_mid ≥ S_mid` → 买 spot @ask + 卖 future @bid（cash-and-carry）；反之 → 卖 spot @bid + 买 future @ask（**须借币**：`residual_risks` 必含 `spot_borrow` / `spot_borrow_fee`，无 `spot_borrow_apr` → `borrow_unavailable` 失效） | `future_bid − spot_ask`（持有到期，`--c1-hold to_expiry`；或 `horizon` 到期前双腿跨点差平仓）。费用：spot 进出 2 次 + future taker（假设 = perp taker）+ 交割费 `--c1-future-settlement-bps` | `fair_value`：stub `S_mid·e^{rT}`（`model=stub`，`anchor_ref`）；`term_structure_curve_ref`：全部到期 + perp 的 mid 基差、`basis_apr_ref`（仅展示）、`role`；`hedge_note`：远月 / perp（不进合成）。残留：结算指数错配 · 近远月流动性不对称 · 展期成本 · gap · margin · basis |
| **C2** | `C2` | funding **期望路径** + 基差确认（`H-C2`；对 A1 的叙事纠正，不是加仓） | spot + perp（方向随 funding 符号；负 funding → 空现货须借币） | `Σ_{h=1..H} f_h × N`，`f_h = f_used·decay^h + f_anchor·(1−decay^h)`：`f_used` 沿用 Cost Engine `FundingLeg`（`min(\|now\|,\|next\|)` 同号，预测翻号 → 0），`f_anchor` = fixture `funding.history` 末 12 期均值（无历史 → `--c2-long-run-rate`），`decay` 默认 0.7；不利入场基差记入毛边；`funding_uncertainty = σ_hist·H + \|now−next\|·H`。**不是 `current×365`** | `funding_model`（stub）、`funding_expected` / `funding_uncertainty`（bps）、`basis_confirmation`：perp/spot mid 基差与期望 funding **同向或在 `--c2-basis-conflict-bps` 内**，否则 `basis_conflict_with_funding_expectation` 失效；`cost_engine.breakeven_funding_rate` 给门槛 |
| **C3** | `C3-B1` / `C3-B3` | B1 / B3 采样门放宽 → 非零样本 → Cost Engine（`H-C3`，**方法假说**） | 复用 §9 `ComboScanner.scan_b1/scan_b3`（B2 不动），**机制唯一且已文档化**：`delta_moneyness_band_plus_5pp_symmetric` —— B1 Δ 带 `[0.15,0.25]→[0.10,0.30]`、moneyness 回退带 `[5%,30%]→[0%,35%]`、B3 Δ 容差 `0.10→0.15`；每到期最多 2 条 call、备兑 / 带翼 / vol_path_haircut / 成本桶**全部不变**；strike 档、±1 expiry 两种机制**未用**（`relaxation.*_mechanism="not_used"`） | 继承 Phase B 记录（`costs_bps` 含 `vol_path_haircut`、`cover`、`paper_delta_sim`…） | 同一快照同时用 Phase B **默认带**跑一遍只计数 → `sample_coverage_B1/B3.baseline_n_phase_b_defaults` 与 `delta_n_from_relaxation`；每条 `sample_coverage{family_key,n_in_window_so_far,gate_n,gate_met_so_far}`；扩样腿仍过 C4 同类滤镜 |
| **C4** | `C4-A2` / `C4-A3` | A2 PCP / A3 box 高流动性滤镜（`H-C4`，**滤镜假说**） | 复用 §8 `ArbScanner.scan_a2/scan_a3`；每条腿：**半价差 ≤ 25 bp（口径：标的名义 bps / 单位，币本位权利金按 spot mid 折算）**、盘口一档 ≥ `1 张 × --c4-contract-size-base`（默认 1.0 = fixture 等价；OKX 实盘按 ctVal 填）、名义粗算 ≥ 50 USDT；单边簿直接不过 | 继承 Phase A 记录（不改 `costs_bps`） | 不过 → `liquidity_ok=false` + `c4_illiquid_leg`（`--c4-mode flag`，默认）或不发（`skip`，计 `skipped.c4_illiquid_skipped`）；`liquidity_filter.legs[*].{half_spread_bps,size_contracts,notional_quote,reasons}`。**C4 不能凭空造出正净边**（A 侧死因是 fees），只减噪声样本 |

C1/C2 的门限 / 持续度 / 深度过滤与 §8 相同（`SafetyBuffer` 四项之和、`PersistenceTracker`、`liquidity_check`），再叠 C4 滤镜；`passes_threshold = net > buffer ∧ persistence ∧ depth ∧ liquidity_ok ∧ 无失效 flag`。数据模型只做了**加法**：`Snapshot.futures`（`kind="future"`，默认空）与 `Funding.history`（默认空），fixture 里为 `"futures": [...]` / `"funding": {"history": [...]}`；A/B 扫描器忽略这两个字段，行为不变（单测覆盖）。

**可证伪指标（summary `falsifiable_metrics`，每窗强制）**：`net_edge_bps_gt_0_rate`、`cost_kill_rate`（**两种口径同时给**：`vs_all` = (gross>0 ∧ net≤0)/全样本，`vs_gross_gt_0` = /gross>0 子集）、`sample_coverage_B1` / `sample_coverage_B3`（`n`、`gate_n`、`gate_met`、`baseline_n_phase_b_defaults`、`delta_n_from_relaxation`、`verdict`）、附 `gross_gt_0_rate` · `median_all_in_cost_bps` · 分族 `max_net_edge_bps` · `calibrated=false`。`hypotheses.H-C1..H-C4` 的 `status` 同样受覆盖门约束。

```bash
uv run python strategies/paper_combo_expansion.py --help
uv run python strategies/paper_combo_expansion.py policy          # 打印门禁 JSON（will_send_http=false 等）

# 离线：回放扩搜合成 fixture（6 快照 × 30s：交割合约 1–3 号基差 +20bp 被成本杀、4–5 号 +54bp net>0 但不过 buffer、
# 6 号贴水 → 反向须借币失效 + perp 低于现货 → C2 基差冲突失效；B1 默认带 0 条 / 放宽后 3 条/快照；1800 call 宽点差+小量 → C4 不过）
uv run python strategies/paper_combo_expansion.py evaluate-fixture --paper-fills \
  --out /tmp/exp.jsonl --summary-out /tmp/exp-summary.json --quiet      # 可证伪指标打到 stderr

# 只跑 C3 / C4，C4 直接丢弃不过滤镜的腿
uv run python strategies/paper_combo_expansion.py scan --expansions C3,C4 --c4-mode skip --print-summary --quiet

# 只读 live 观察：OKX 公共 GET（无密钥）：spot / perp / funding / 最近 2 个交割合约 / 2 到期 × 8 行权价，5 个快照 × 20s
uv run python strategies/paper_combo_expansion.py scan --source okx-public --fut-family ETH-USDT --max-futures 2 \
  --n-strikes 8 --max-expiries 2 --samples 5 --interval-sec 20 --c4-contract-size-base 0.1 \
  --out /tmp/exp-live.jsonl --summary-out /tmp/exp-live-summary.json
```

fixture 一窗的结果只是代码路径证据：330 条，net>0 = 3 条（全部 C1 合成基差，未过 buffer、未标定），C2 / C3 / C4 在覆盖达标（B1 n=18 vs 默认带 0；B3 n=12）下均为 `cost_veto_on_window`。**这不是市场证据，更不是任何一族「有边」或「已死」的结论。**

**分阶段（不得跳级）**：① 只读指标（`--source fixture` / `--source okx-public`）→ ② 纸面成交（`--paper-fills`）→ ③ `04-risk` 复审 → ④ **用户确认**后才允许讨论任何执行请求。**本模块止步于 ②，且 stub 模型 `calibrated=false` 时禁止升级「可交易」话术。** 明确不做：裸卖波动率、跨所延迟套利、多腿 live IOC、动态对冲 HTTP、mid / mark 报净边、`current×365`、并入网格主线、>1x、BTC（未点名）、覆盖不足时的任何「证伪 / 有效」结论。Fair-value（路线图 P1）/ Funding expectation（P2）模块落地后（各自另批 `04-risk`），C1 / C2 的 stub 应被替换而不是并存。

### 13. E2 Phase A · demo OMS 连通性 dry-run（`strategies/e2_demo_oms.py`）

**Phase A = 连通性 dry-run，只构建意向、不发任何 HTTP。** 首个策略标签 **E2-G1**：ETH-USDT 现货分层限价梯（Phase A 只做意向构建器）。对应 `04-risk` E2 约束：`will_send_http` 默认 `false`；**Phase B（真实 demo 下单）不在本 PR 范围**，须另开 `04-risk` 评审 + 用户在聊天里明确确认 + 新的执行模块；官方网格 Bot **A / B′ / C 不动**（本模块不持有任何 Bot 标识）。

| 门禁 | 实现 |
|------|------|
| 默认不发 HTTP | `GridIntentConfig.will_send_http=False`、`DemoOms(will_send_http=False)`、`POLICY.will_send_http=false`；模块源码**没有** `urllib` / `http` / `socket` / `httpx` / `requests` / `okx` import（单测 + CI 用 AST 扫描），也不 import 只读客户端 |
| `will_send_http=True` → 拒绝 | 缺 `OKX_SIMULATED=1` + 三个 `OKX_*` 变量 → `DemoEnvCheckFailed`；**齐全仍拒绝** → `PhaseASendNotImplemented`（env 齐 ≠ 许可；Phase A 无发送代码）。`DemoOms.place_order / amend_order / cancel_order` 调用即抛 `SendRefused` |
| 风控预检（依次，先判发送门） | `live=True` → `LiveFlagRefused`；意向元数据缺 `x-simulated-trading=1` / `venue=okx_demo` / `demo_only` → `SimulatedIntentMetadataMissing`；非 `ETH-USDT` → `InstrumentNotApproved`；`tdMode≠cash` 或 `lever≠1` → `TdModeForbidden`；`market` / `ioc` / `fok` / `optimal_limit_ioc` → `OrderTypeForbidden`（只许 `limit` / `post_only`）；`auto_reband=True` → `AutoRebandRefused`；档数 ∉ 1..10 → `LevelsOverCap`；总名义 ∉ (0, 100] USDT → `NotionalOverCap`（配置值与按 lot 切片后的实际值都查）；最高买档 ≥ 参考价（会吃单）→ `BuyLevelAboveReference`；无 SL → `StopLossMissing`；SL 不是严格低于最低档的绝对价 → `StopLossPlacement`；单档量 < `minSz` → `SizeBelowMin` |
| 确定性 | 价格 / 数量用 `Decimal` 按静态 `tickSz=0.01` / `lotSz=0.000001`（**未联网拉取**，`instrument.source=static_default_not_fetched`）量化；`clOrdId` = `e2g1a` + sha256 前 16 位；`plan_id` = orders 规范化 JSON 的 sha256；不传 `now` 就没有时间戳字段 → 同配置两次构建逐字节相等 |

```bash
# 默认：ref 2500、买梯 2300–2500 十档（挂单 2300…2480，配对出场 2320…2500）、总名义 100 USDT、SL 2150；stdout JSON + 落盘
uv run python strategies/e2_demo_oms.py plan --out /tmp/e2-g1-phase-a.json
# 拒绝路径全部退出码 3，输出 {"refused": true, "code": ..., "will_send_http": false, "orders_placed": 0}
uv run python strategies/e2_demo_oms.py plan --will-send-http     # DemoEnvCheckFailed / PhaseASendNotImplemented
uv run python strategies/e2_demo_oms.py plan --live               # LiveFlagRefused
uv run python strategies/e2_demo_oms.py plan --total-notional 500 # NotionalOverCap
uv run python strategies/e2_demo_oms.py plan --sl-trigger-px ""   # StopLossMissing
uv run python strategies/e2_demo_oms.py policy                    # 离线策略
uv run python strategies/e2_demo_oms.py check-env                 # 只报 set/unset，不回显任何值
```

产出 `e2_demo_oms_intent_plan_v1`：`orders[]` 每档 `instId / tdMode=cash / side=buy / ordType / px / sz / clOrdId / tag / notional_usdt / paired_exit_px`（配对卖出**只是价格标注**，Phase A 不构建卖单）、`totals`（实际名义 ≤ 100）、`stop_loss.basis=absolute_px`、`prechecks[]` 通过清单、`policy`、`phase_b_requires`、`risk_notes`、`submit_result.sent=false`。`endpoint_label` 只是给评审看的字符串，模块里没有对应的调用。

**本 PR 明确不含且被代码拒绝**：任何写端点（下单 / amend / cancel）、提现 / 划转、市价 / IOC 扫单、自动重画区间、默认 `will_send_http=true`、T1 / T2 套利 live 路径、对官方 Bot 的任何操作。**Phase A 绿 ≠ 允许 demo 下单**；Phase B 另案。

## CI

GitHub Actions 工作流 [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 在 **pull_request** 以及 **push 到 `master`** 时跑纸面冒烟：

**job `paper-smoke`（离线，阻塞）**

1. `astral-sh/setup-uv@v10.1.0` 安装 uv，`uv sync --locked --no-dev` 按锁文件同步运行时（标准库 + pin 死的 `python-okx`；不装 ruff、不装别的）。
2. 十三个脚本的 `--help` 能通过 `uv run --locked --no-dev python …` 启动（Python 3.12）。
3. **OKX SDK 审计**：AST 扫描 `tools/ strategies/ tests/` 下所有 `.py`，import 的 `okx*` 模块必须 ⊆ `{okx, okx.MarketData, okx.Account, okx.Grid}`；导入门面后 `sys.modules` 里不得出现 `okx.Trade` 等写模块；`policy` 子命令可离线运行。
4. `local_paper_grid.py --source synthetic`：断言 `venue=local_paper`、`will_send_http=false`、`lever=1`、`buy/sell/arbitrage > 0`、`total − fees == fee_after`。
5. `grid_ab_compare.py` 用 `import` 与 `subprocess` 两种引擎各跑一次合成路径：断言两臂 `baseline`/`B1` 都在、`lever=1`、必填字段齐全、`arbitrage_num > 0`、两臂 K 线数一致、两引擎 metrics 相等、`adopted=false`、`bot_changed=false`、H-A/H-B/H-C 都有评分、H-C 无否决臂且 `add_position_proposals_allowed=false`；并把 markdown 表打到日志。
6. `okx_readonly_client.py private-status` 与 `okx_sdk_readonly.py private-status` 在清空 `OKX_*` 环境后都必须打印 `skipped=true`（无密钥 stub），`policy` 里 `will_send_http/order/amend/withdraw/transfer` 全为 false；SDK 门面给占位（非密钥）live 风格 key 但缺 `OKX_SIMULATED=1` 时必须退出码 3 且不回显。
7. `tools/cost_engine.py --full`（Cost Engine v0 fixture 用例离线回放，无网络、无密钥）：断言 `all_ok`、报告 `observe_only` / `will_send_http=false` / `trading_http` 全 false / `calibrated=false` / `tradable_claim_allowed=false`、三条拒绝用例各命中对应异常（年化毛边 → `NaiveAnnualizationRefused`、mark 作可执行价 → `ExecutablePriceViolation`、无依据标定 → `CalibrationClaimRefused`）、每条结果八个核心分量齐全、`all_in = Σ 分量`、`net = gross − all_in`、`annualized=false`、有 breakeven 的 `headroom × H ≈ net`。
8. `paper_arb_scanner.py --source fixture --paper-fills`（离线 fixture 回放，无网络、无密钥）：断言每条记录 `action=observe_only`、`will_send_http=false`、腿的 `price_type∈{bid,ask}` 且买 ask / 卖 bid、A1 为 `relative_value`、A2/A3 为 `identity_approx*`、`costs_bps.total` 存在、`paper_fill.order_sent=false`、三个家族都出现、卖箱永不 pass、`cost_engine` 块 `observe_only` / `calibrated=false` / `all_in_cost_bps == costs_bps.total` / 只有 A1 有 breakeven、summary 里 `trading_http` 全 false、`safety_buffer.calibrated=false`、`cost_engine.records` = 记录数且 breakeven 条数 = 快照数、H-A1/H-A2/H-A3 都有状态。
9. `paper_combo_scanner.py --source fixture --paper-fills`（Phase B 双到期合成 fixture 回放，无网络、无密钥）：断言每条记录 `action=observe_only`、`will_send_http=false`、`phase=B`、`taxonomy=relative_value`、`combo_id` 与 `family` 对应、`hedge_mode` 在白名单、`residual_risks` 含家族强制名单、腿 bid/ask 且买 ask / 卖 bid、每条短期权腿都有 `cover`、`paper_delta_sim.live_hedge_http=false`（B2 必 `enabled=true`）、`costs_bps.vol_path_haircut` 与 `safety_buffer.vol_path_haircut_bps` 存在、`cost_engine` 块一致且含 `vol_path_haircut`、只有 B1 有 breakeven、记录文本不含 `risk_free` / `无风险` / `稳赚` / `guaranteed`、三家族齐全；summary 里 `live_delta_hedge=false`、`trading_http` 全 false、`calibrated=false`（buffer 与 cost_engine 两处）、`leverage_concept=1`、H-B1/H-B2/H-B3 都有状态、A1 同窗对照条数 = 快照数、`mainline_unchanged`。
10. `paper_triangle_scanner.py --source fixture`（T1 三腿合成 fixture 回放，无网络、无密钥）：断言每条记录 `action=observe_only`、`will_send_http=false`、`menu_id=T1`、`taxonomy=same_venue_microstructure`、三腿恰为白名单 `ETH-USDT` / `BTC-USDT` / `ETH-BTC` 且 bid/ask 买 ask / 卖 bid、`leverage_concept=1`、`residual_risks` 含非原子三腿 / 残留库存、`risk_flags` 含 `non_atomic_three_leg`、`gross_edge_bps` / `net_edge_bps` / `costs_bps.total` **等于** `cost_engine` 块对应字段、`fees` 与 `half_spread_slip` > 0、`book_sync.max_skew_ms=200`、`liquidity.max_half_spread_bps=15`、带 `stale_book` / `illiquid` 的记录 `passes_threshold=false`、`synthetic_fill.order_sent=false`、文本不含 `risk_free` / `无风险` / `稳赚` / `guaranteed`、双向都出现；summary 里 `trading_http` 全 false、buffer 与 cost_engine 两处 `calibrated=false`、`stale_book` / `illiquid` / `cost_killed` 计数 > 0（fixture 覆盖了这些路径）、`net_positive_rate` / `cost_kill_rate` / `synthetic_fill_success_rate` 非空、`H-T1` 有状态、`mainline_unchanged`；**stale-fix v1 追加**：每条记录 `book_sync.sync_gate="strict"` 且 fixture 记录 `book_sync.fetch=null`、`sync_report.sync_gate="strict"` / `gate_unchanged=true` / `max_skew_ms=200`、`subsets.all == metrics`、`synced + stale_or_missing == records`、`synced` 子集 `stale_book=0`、`stale_or_missing` 子集 `passes_threshold=0`、`H-T1.synced_subset` 存在、summary 文本不含 `relaxed`；CLI `--help` 含 `--fetch-mode` 与 `--sync-retries` 且默认 `parallel` / `0`。
11. `paper_combo_expansion.py evaluate-fixture --paper-fills` + `policy`（C1–C4 扩搜合成 fixture 回放，无网络、无密钥）：断言 policy `will_send_http=false` / `live_execution=false` / `trading_http` 全 false / 1x / `relative_value` / C4 半价差上限 25 / 覆盖门 N=5；每条记录 `action=observe_only`、`will_send_http=false`、`phase=C`、`taxonomy=relative_value`、`combo_id` 以 `expansion_id`（C1–C4）开头、`calibrated=false`、`net_edge_source` 为 cost engine、`cost_engine` 块 `observe_only` / `calibrated=false` / `annualized=false` 且 `all_in == costs_bps.total`、`net == net_edge_bps`、`gross == gross_edge_bps`、腿 bid/ask 且买 ask / 卖 bid、C1/C2 ≤2 腿且 `model=stub` 且 `hedge_note` 全 `tradeable_default=false`、C1 `fair_value` stub 且不进净边、C2 `annualized_used_in_net_edge=false` 且有 breakeven 且基差冲突必失效、C3 只含 B1/B3 且每条短期权有 `cover`、C4 只含 A2/A3（`taxonomy_base=identity_approx`，≤4 腿）、`liquidity_ok=false` 者必不 pass 且带 `c4_illiquid_leg`、`paper_fill.order_sent=false`、文本无禁词、四族齐全；summary 里 `live_execution=false`、`underlying=ETH` 且 `allow_btc=false`、`cost_engine.mandatory` 且条数一致、`falsifiable_metrics` 四项齐全且 `calibrated=false`、B1 覆盖基线 0 且 n≥5、verdict 只在三个允许字串内、H-C1..H-C4 都有状态、C3 机制名固定、C4 有被标 illiquid 的记录、`mainline_unchanged`。
12. `e2_demo_oms.py plan`（E2 Phase A demo OMS，清空 `OKX_*` 后离线跑，无网络、无密钥）：断言 `schema=e2_demo_oms_intent_plan_v1`、`E2-G1` / `phase=A` / `mode=dry_run`、`will_send_http=false` / `http_sent=false` / `orders_placed=0` / `submit_result.sent=false`、policy `trading_http` 全 false / `lever=1` / `auto_reband=false`、意向元数据 `x-simulated-trading=1` + `venue=okx_demo`、`instrument.source=static_default_not_fetched`、1..10 档且实际名义 ≤ 100、每档 `ETH-USDT` / `cash` / `buy` / `limit|post_only` 且 `SL < px < ref`、`prechecks` 全过、`--out` 落盘与 stdout 同 `plan_id`、文本无禁词 / 无 `algoId`；`--live` / `--will-send-http` / `--auto-reband` / 超名义 / 超档数 / 非 ETH-USDT / 无 SL 各自**退出码 3** 且 `orders_placed=0`；给占位（非密钥）demo env + `--will-send-http` 仍退出 3（`PhaseASendNotImplemented`）且不回显；AST 扫描模块源码无 `urllib` / `http` / `socket` / `httpx` / `requests` / `okx` import，`unittest.mock` 挂在 `DemoOms.place_order` 上断言 build → submit 全程**从未调用**。
13. `python -m unittest discover -s tests`（无网络）。
14. 各跑一遍默认参数：`okx_grid_dry_run.py` 必须含 `"will_send_http": false` 和 `method: PRINT_ONLY`。

**job `public-read-smoke`（出网，`continue-on-error: true`，不阻塞）**

对 OKX **公共** `ticker` / `candles` 做只读 GET（无密钥；`OKX_*` 显式置空），先走标准库客户端、再走官方 SDK 门面，断言 `auth=none`、`read_only=true`、K 线按时间升序；再把这 12 根公开 K 线经 CSV 回放喂给 `grid_ab_compare.py`。这一步红只代表「runner 到 OKX 公共 API 不通」，不代表任何交易发生。

**CI 绿 ≠ 实盘。** 工作流不注入 Secrets、不 amend、不提现、不下单；唯一出网的是上面那个只读公共行情 GET。**推代码 ≠ live。** 不要在 Actions 里配置 API key / `.env`。

## 笔记

- [`notes/2026-09-16-code-screen-from-reading-pack.md`](notes/2026-09-16-code-screen-from-reading-pack.md) — 哪些可进代码、哪些明确不做
- [`notes/2026-09-16-ft-source-audit.md`](notes/2026-09-16-ft-source-audit.md) — Freqtrade 对照：借什么、不借什么
- [`notes/2026-09-16-v2-tools.md`](notes/2026-09-16-v2-tools.md) — v2 旁路与冒烟口径

## 远程仓与流程

- GitHub：`https://github.com/littleboss/cryptowang`
- 流程：本地草稿 → 本仓（风控执行）→ 运维部署（Atlas）
- **推代码 ≠ 实盘**；改参 / 部署触实盘须用户明确确认
