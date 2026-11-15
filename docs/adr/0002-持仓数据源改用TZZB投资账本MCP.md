# ADR 0002：同花顺同步的数据源、canonical 代码与独立扩展包分发

- Status：Implemented 2026-11-15
  - TZZB 投资账本 MCP 作为持仓/交易主源：**Implemented 2026-11-15**。
  - canonical 代码口径、板块入池、零弹框、原生预览、独立扩展包：**Implemented 2026-11-15**。
- Date：2026-11-15
- Owner：koolook / MOSS
- Related：BOSS 指令 `m00782`（「以后就做拓展包了，顺带帮我更新一下tsp」）、`m00879`（继续重建）、`m02046`（「为什么没有实现」）、`docs/extension-packages.md`、`docs/page-sdk.md`

## 1. 背景与范围纠正

BOSS 原文（历史摘抄）：

- `A，所有改动要和tsp适配，所以同步过去的自选，持仓，ai选股等，都要可以使用tsp原生自带的股票查看功能查看。`
- `B，1.`（空板块采用方案 1：跳过空板块）
- `C，完成了。`（17660 进程内代理）
- `bug：一直弹终端框，只能看到代码，我不确定有没有反应`
- `新功能：我配置了同花顺投资账本的MCP ... 你看看能不能通过他读取我的持仓以及其他交易信息，并分析替代原来的读取持仓等信息的方案的可行性，告诉我哪个方法更好，原来的方法还是会抢我前台`

早期交付把「分析替代方案」处理成只给结论，并在 `0ded399` 回退事故后以「不留半实现」为由撤回了 `tzzb.py`。BOSS 后续质疑「为什么没有实现」，确认真实意图是补齐主源。本 ADR 记录范围纠正后的最终状态：TZZB MCP 主源已经接入代码、端点、扩展包、冻结运行目录与验证脚本。

## 2. TZZB MCP 主源（Implemented 2026-11-15）

### 2.1 数据源与账户

TZZB 投资账本 MCP 已配置，`promptAccountSelection(scope="stock")` 返回 `fundKey=193735580`、`lastSyncTime=2026-11-13 17:07:58`。新鲜实测读通：

- `queryStockPosition`：`availableFunds=801.5000`、`positionRatio=0.9331`、`totalMarketValue=11188.00`、`totalAsset=11989.5000`、`totalLiability=0`；有效持仓 4 只：`600327 大东方 500 / 000034 神州数码 100 / 000819 岳阳兴长 200 / 002647 仁东控股 400`。
- `queryPnlSummary`：`totalProfitLoss=712.2704`，含 11 日净值趋势与上证指数趋势。
- `queryClearedPosition`：30 条清仓记录。
- `queryMoneyHistory`：资金流水分页可用。
- `queryAssetChangeSummary`：`money_in 15000`、`money_out 1000`、`end_asset 11989.5`。
- `queryDayProfitSummary`：逐品种盈亏 8 行。

### 2.2 实现边界

- `backend/app/custom/ths_sync/tzzb_source.py` 是 TZZB MCP 主源模块。它通过本机 `mcporter call TZZB.<tool> --args <json> --no-oauth --output json` 取纯 JSON。
- 所有子进程使用 `shell=False`、`CREATE_NO_WINDOW`、`STARTF_USESHOWWINDOW`、`SW_HIDE`，不弹终端框、不抢前台。
- `positions_from_payload()` 只接受 6 位 A 股代码，过滤 `K60080` 类占位与 `holdQuantity <= 0`，把 `stockCode/stockName/holdQuantity/costPrice/currentPrice/marketValue/holdProfit` 转成 `reader.Position`。
- `read_positions()` 并行取账户与持仓；`read_snapshot()` 并行取账户、持仓、盈亏；`read_trade_summary()` 并行取清仓、资金流水、资产变动、逐日盈亏。
- `service.read_positions_any(*, allow_screen: bool = True)` 的默认链为「TZZB MCP 主源 → 17660 代理 → 屏幕 OCR」；`build_status()` 使用 `read_positions_any(allow_screen=False)`，状态页不再触发屏幕 OCR。
- `GET /api/custom/ths-sync/tzzb`（真包内为 `/api/extensions/koolook.ths-sync-pack/tzzb`）返回 `ok`、`source="TZZB MCP"`、`positions_count>=4`、账户、`lastSyncTime`、盈亏、清仓、资金流水、资产变动、逐日盈亏。
- 降级保留：MCP/Node/登录态/账本限流失效时，手动同步仍可走 17660 代理；只有允许屏幕时才走 OCR。

### 2.3 方案比较结论

TZZB MCP 作为持仓/交易主源优于「17660 代理 + 屏幕 OCR」：字段覆盖持仓、盈亏、清仓、资金流水、资产变动；纯 CLI/HTTP 通道不依赖同花顺前台窗口；持久化账本数据更适合交易信息展示。代价是数据新鲜度取决于账本 `lastSyncTime`，且需要 Node、mcporter 与登录态可用。因此保留代理/OCR 降级，并在状态与 `/tzzb` 端点暴露 `mcp_ok`、`mcp_errors`、`lastSyncTime`。

## 3. 已实施的前置工程（Implemented 2026-11-15）

### 3.1 canonical 代码口径

TSP 原生个股预览 `frontend/src/lib/stockPreviewStore.ts` 只认 `^\d{6}\.(SH|SZ|BJ)$`。因此：

- `backend/app/custom/ths_sync/reader.py:74` 有 `_TSP_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.I)`。
- `backend/app/custom/ths_sync/reader.py:77 def to_tsp_symbol(code: str) -> str | None:` 把同花顺裸码转 canonical：`6` → `.SH`，`0/3` → `.SZ`，`8/4` → `.BJ`，幂等。
- `backend/app/custom/ths_sync/reader.py:94 def to_ths_code(code: str) -> str | None:` 把 canonical 转回同花顺裸码，幂等。
- `backend/app/custom/ths_sync/service.py:107 STATE_VERSION = 3`；`run_once()` 启动时 `_canonicalize_existing()` 把历史裸码迁移成 canonical；入池集合全部 canonical；写回同花顺前用 `reader.to_ths_code()` 转回裸码。
- `build_status()` 给每条持仓 dict 追加 `d["symbol"] = reader.to_tsp_symbol(d.get("code") or "") or d.get("code")`，前端 `openStockPreview({ symbol })` 可用。
- `frontend/src/custom/ths-sync/Page.tsx` 持仓表 `row.code` 是按钮：`openStockPreview({ symbol: row.symbol || row.code, name: row.name })`。

### 3.2 板块入池与空板块策略

- `NOTE_BLOCK = "同花顺板块"`，`SYNC_NOTES = (NOTE, NOTE_BLOCK)`，`GROUP_BLOCK_PREFIX = "同花顺·"`。
- `reader.list_blocks(user)` 枚举同花顺板块；`blk_count <= 0` 的空板块跳过（BOSS B=方案 1）。
- 非空板块成分转 canonical 后入池，分组名 `同花顺·<板块名>`；`desired_effective` 纳入 `desired_block | keep_block`，防误删。
- 实况：自选 47 只、ai选股 12 只、watchlist 59 行全 canonical；分组 `同花顺自选` / `同花顺持仓` / `同花顺·ai选股`。

### 3.3 零弹框与不抢前台

- `backend/app/custom/ths_sync/writer.py:87 def ths_running() -> bool:` 从 `subprocess.run(["powershell", ...])` 改为纯 `ctypes` Toolhelp32 进程快照：`CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)` + `Process32FirstW/Process32NextW`。
- 自动轮询只跑自选（`run_once(with_positions=False)`），不碰 17660；持仓由 `/sync/positions` 手动一键触发，主源走 TZZB MCP，代理/OCR 仅降级。
- 实测 `no powershell/cmd child of TSP []`。

### 3.4 写回只加不减

`C:/同花顺远航版/bin/users/爱因思睿/SelfStockCache.json` 写回基准固定取本轮 `self_codes`，`target_codes = sorted({c for c in (self_codes or []) if reader.is_a_stock(c)} | {c for c in native_codes if c})`。`ths_written_selfstock` 只作展示，不参与回环排除；来源读取失败时跳过写回。此前回环排除曾导致 40→5 截断，禁止恢复。

## 4. 独立扩展包分发（Implemented 2026-11-15）

### 4.1 真实扩展契约

TSP 不识别仓库根 `extensions/koolook.ths-sync/manifest.json`；`backend/app/extensions/loader.py:24` 只扫 `app.custom`。真扩展包必须是 `tick-extension-package`：

- 根目录 `extension.yaml`：`format: tick-extension-package`、`format_version: 1`、`extension_api_version: 1`、`backend: backend.extension`、`frontend: frontend/extension.mjs`。
- `backend/extension.py`：导出 `EXTENSION_ID`、`EXTENSION_API_VERSION`、同步 `def setup(registrar):`、同步 `def startup(context):`。
- 外置接口必须位于 `/api/extensions/<id>/`（`backend/app/extensions/packages.py:336-340`）。
- `frontend/extension.mjs`：Page Pack SDK v1 自包含 ES 模块，`default.id` 与包 id 相同，`apiVersion: 1`，`ctx.ui.openStockPreview` 可用。
- ZIP 限制：20 MiB / 80 MiB / 1000 条目，拒绝 `.pyc`、`__pycache__`、符号链接、`plugin.yaml` 混用。

### 4.2 包 ID 决策

源码级 custom 扩展 `backend/app/custom/ths_sync/__init__.py:29` 已占用 `EXTENSION_ID = "koolook.ths-sync"`。`backend/app/extensions/packages.py:320-321` 明确：外置包 ID 与 registry 已有 ID 相同会抛 `扩展 ID 与主程序已有扩展冲突`。

决策：真包用独立 ID `koolook.ths-sync-pack`，同步改：

- `extension.yaml` 的 `id`
- `backend/extension.py` 的 `EXTENSION_ID`
- `backend/ths_sync/routes.py` 的 `router = APIRouter(prefix="/api/extensions/koolook.ths-sync-pack", tags=["ths-sync-pack"])`
- `frontend/extension.mjs` 的 `PACK_ID` / `API` / route path `/ths-sync-pack`

理由：不改 BOSS 正在运行的源码级 custom 扩展 ID，避免运行中 TSP 和冻结目录行为突变；真包可以独立 inspect / install / update。代价：装包后不能与 custom 扩展同时启用，否则两个后台线程会同时写 `ths_sync_state.json` 与 `SelfStockCache.json`。安装说明必须写明二选一。

### 4.3 构建与校验

持久脚本：

- `tools/build_ths_sync_extension.py`：生成 `release/koolook.ths-sync-pack-1.0.0.zip`，`MODULES` 包含 `tzzb_source.py`。
- `tools/verify_ths_sync_extension.py`：本地契约 + 运行期 `POST /api/import-center/inspect` 校验，断言 MCP 主源与 `/tzzb` 端点。
- `D:/TSP/_verify/final_verify_20261115.py`：运行期 HTTP、冻结目录、ADR、ZIP、无窗口、canonical、状态页与 `/tzzb` 端点终检。

产物放 `release/` 而非 `dist/`，因为 `.gitignore:16 dist/` 会忽略 ZIP；`release/` 当前未被忽略，便于作为 Release 资产交付。

## 5. Baseline Sync

- canonical owner：`backend/app/custom/ths_sync/` 是源码级实现；`tools/build_ths_sync_extension.py` 是包产物唯一构建入口；`docs/adr/0002-持仓数据源改用TZZB投资账本MCP.md` 是本决策记录。
- source-of-truth：持仓/交易主源为 `tzzb_source.py` 与 TZZB MCP；17660 代理与屏幕 OCR 是降级通道，不再担任默认主源。
- external install surface：包 ID `koolook.ths-sync-pack`，外置 API `/api/extensions/koolook.ths-sync-pack/`，前端 Page Pack route `/ths-sync-pack`。
- duplicate owners：`extensions/koolook.ths-sync/` 是历史迁移副本，已清理；`manifest.json` 为无效旧清单，已从主交付面移除。
- baseline result：运行中的源码级 custom 扩展和真包不可同时启用；安装说明必须写二选一。

## 6. 后续动作

### 6.A 已完成

1. A/B/C、bug4 弹框、canonical 原生预览、板块入池、写回只加不减、零弹框、前端构建、冻结目录部署、重启、E2E 全绿。
2. MCP 可行性分析与方案比较。
3. 补齐 `tzzb_source.py`、MCP 主源链、交易信息端点、状态页 MCP 字段与降级语义。
4. 真 `tick-extension-package` ZIP 构建脚本、校验脚本、HTTP inspect 校验。
5. 运行目录 `D:/TSP/_internal` 已更新并验证。

### 6.B 已完成：MCP 换主源实施清单

1. 主源/降级顺序定为「TZZB MCP → 17660 代理 → 屏幕 OCR」；`lastSyncTime` 与 `mcp_errors` 进入状态页。
2. 新增 `tzzb_source.py`，返回 `list[reader.Position]`、持仓快照与交易/盈亏摘要，service 只调用模块函数。
3. `build_status()` 使用 `read_positions_any(allow_screen=False)`，状态页不触发 OCR。
4. RED/GREEN harness 覆盖字段转换、主源顺序、状态禁 OCR、端点、ZIP、冻结目录、ADR 与运行期 `/tzzb`。
5. 保留 17660/OCR 降级；`allow_screen=False` 时只到代理为止。
6. 前端状态、扩展包构建脚本、校验脚本、最终验证脚本同步更新，重新 `inspect()`。
7. BOSS 质疑后默认主源已切换并写入代码与文档。

## 7. 风险与约束

- `git reset HEAD && git checkout -- .` 曾丢未提交成果；后续改动先 commit/stash。
- `queryDayCalendarSummary` 存在 `服务器繁忙` 限流；MCP 主源保留降级与错误字段，不把限流伪装成空数据。
- `SelfStockCache.json` 写回必须只加不减；禁止恢复 `ths_written_selfstock` 回环排除。
- `extension.yaml` 与 `plugin.yaml` 不得同时存在；包内不得有 `.pyc`/`__pycache__`。
- 真包与源码级 custom 扩展二选一启用，避免双线程写同一状态与自选缓存。
- TZZB 数据新鲜度取决于账本 `lastSyncTime`；状态页与 `/tzzb` 端点必须保留该字段。

## 8. ADR Compact Output

- Aegis Visibility：本 ADR 修正了「只分析不落地」的范围收窄，明确 TZZB MCP 主源已实现。
- Decision Candidate：持仓/交易主源从 17660/OCR 切换到 TZZB MCP，同时保留降级链。
- ADR Gate：改动涉及 canonical owner、公开端点、包产物与 source-of-truth，需记录。
- Retro / Memory Filter：记录的是已执行且可验证的持久决策，不记录临时进度。
- ADR Action：amend。
- Owner Surface：canonical source、运行目录、ZIP、ADR、构建/校验/最终验证脚本。
- Baseline Sync：见 §5。
- Boundary：技术记录不替代运行期验证；完成声明以新鲜验证输出为准。
