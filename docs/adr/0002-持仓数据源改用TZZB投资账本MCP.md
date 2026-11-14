# ADR 0002：同花顺同步的数据源、canonical 代码与独立扩展包分发

- Status：分轨
  - 换源决策（TZZB 投资账本 MCP 作为持仓/交易主数据源）：**Proposed**，未实现。
  - 前置工程（canonical 代码口径、板块入池、零弹框、原生预览、独立包骨架）：**Implemented 2026-11-15**。
- Date：2026-11-15
- Owner：koolook / MOSS
- Related：BOSS 指令 `m00782`（「以后就做拓展包了，顺带帮我更新一下tsp」）、`m00879`（继续重建）、`docs/extension-packages.md`、`docs/page-sdk.md`

## 1. 背景

BOSS 的原话（摘抄，历史记录）：

- `A，所有改动要和tsp适配，所以同步过去的自选，持仓，ai选股等，都要可以使用tsp原生自带的股票查看功能查看。`
- `B，1.`（空板块采用方案 1：跳过空板块）
- `C，完成了。`（17660 进程内代理）
- `bug：一直弹终端框，只能看到代码，我不确定有没有反应`
- `新功能：我配置了同花顺投资账本的MCP ... 你看看能不能通过他读取我的持仓以及其他交易信息，并分析替代原来的读取持仓等信息的方案的可行性，告诉我哪个方法更好，原来的方法还是会抢我前台`

后续 `0ded399` 曾把上午未提交的成果整体回退；本 ADR 按当前真实代码重新记录决策状态。

## 2. 已实施的前置工程（Implemented 2026-11-15）

### 2.1 canonical 代码口径

TSP 原生个股预览 `frontend/src/lib/stockPreviewStore.ts` 只认 `^\d{6}\.(SH|SZ|BJ)$`。因此：

- `backend/app/custom/ths_sync/reader.py:74` 加 `_TSP_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.I)`。
- `backend/app/custom/ths_sync/reader.py:77 def to_tsp_symbol(code: str) -> str | None:` 把同花顺裸码转 canonical：`6` → `.SH`，`0/3` → `.SZ`，`8/4` → `.BJ`，幂等，非 A 股返回 `None`。
- `backend/app/custom/ths_sync/reader.py:94 def to_ths_code(code: str) -> str | None:` 把 canonical 转回同花顺裸码，幂等。
- `backend/app/custom/ths_sync/service.py:107 STATE_VERSION = 3`；`run_once()` 启动时 `_canonicalize_existing()` 把历史裸码迁移成 canonical，随后 `desired_self` / `desired_pos` / `desired_block` 全部用 canonical 入池；出向 `writer.write_block()` 与 `writer.write_selfstock()` 前用 `reader.to_ths_code()` 转回裸码。
- `build_status()` 给每条持仓 dict 追加 `d["symbol"] = reader.to_tsp_symbol(d.get("code") or "") or d.get("code")`，让前端 `openStockPreview({ symbol })` 可用。
- `frontend/src/custom/ths-sync/Page.tsx` 持仓表 `row.code` 从纯文本改成按钮：`openStockPreview({ symbol: row.symbol || row.code, name: row.name })`。

### 2.2 板块入池与空板块策略

- `NOTE_BLOCK = "同花顺板块"`，`SYNC_NOTES = (NOTE, NOTE_BLOCK)`，`GROUP_BLOCK_PREFIX = "同花顺·"`。
- `reader.list_blocks(user)` 枚举同花顺板块；`blk_count <= 0` 的空板块跳过（BOSS B=方案 1）。
- 非空板块成分转 canonical 后入池，分组名 `同花顺·<板块名>`；`desired_effective` 纳入 `desired_block | keep_block`，防误删。
- 当前实况：自选 47 只、ai选股 12 只、watchlist 59 行全 canonical；分组 `同花顺自选` / `同花顺持仓` / `同花顺·ai选股`。

### 2.3 零弹框与不抢前台

- `backend/app/custom/ths_sync/writer.py:87 def ths_running() -> bool:` 从 `subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-Process happ,xiadan,xiadan-plus ...`).Count` 改为纯 `ctypes` Toolhelp32 进程快照：`CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)` + `Process32FirstW/Process32NextW`。
- 自动轮询只跑自选（`run_once(with_positions=False)`），不碰 17660；持仓由 `/sync/positions` 手动一键触发，代理优先、屏幕 OCR 兜底。
- 实测 `no powershell/cmd child of TSP []`。

### 2.4 写回只加不减

`C:/同花顺远航版/bin/users/爱因思睿/SelfStockCache.json` 写回基准固定取本轮 `self_codes`，`target_codes = sorted({c for c in (self_codes or []) if reader.is_a_stock(c)} | {c for c in native_codes if c})`。`ths_written_selfstock` 只作展示，不参与回环排除；来源未读成功时跳过写回。此前回环排除曾导致 40→5 截断，禁止恢复。

### 2.5 运行目录更新与 E2E

新鲜证据：`RESULT: E2E PASS health/static/dry/real/idempotent/watchlist/groups/positions/status/auto/zero-truncation/no-popups`；`D:/TSP/_internal` 已更新；`watchlist rows=59 canonical=59 bare6=0`；`SelfStockCache.json` 47→47 零截断；`state_version=3 auto=True`；无 powershell/cmd 子进程弹框。

## 3. MCP 换源分析（Proposed，未实现）

### 3.1 实测能力

TZZB 投资账本 MCP 已配置，账户 `fundKey=193735580`、`lastSyncTime=2026-11-13 17:07:58`。实测读通：

- `queryStockPosition`：8 行；有量 4 只 `600327 大东方 500 / 000034 神州数码 100 / 000819 岳阳兴长 200 / 002647 仁东控股 400`；另 4 只 `holdQuantity 0`。
- `queryPnlSummary`：`totalProfitLoss 712.2704`、11 日净值、8 指数。
- `queryClearedPosition`：30 条。
- `queryMoneyHistory`：`maxPage 4`。
- `queryDayProfitSummary`：8 行。
- `queryAssetChangeSummary`：`money_out 1000`。
- `queryMonthCalendarSummary`：`202611 +712.2704`。
- `queryDayCalendarSummary` 曾返回 `{"error_code":"-2","error_msg":"服务器繁忙,请稍后再试"}`，限流而非能力缺失。

后端直连路径：`C:/Users/koolook/.mcporter/tzzb_bridge.mjs` 是 DSH 客户端 stdio 桥；TSP 后端（同一 Windows 用户）可复用 `mcporter call TZZB.<name> --args ... --no-oauth --output json`，`windowsHide: true`。实测命令：`"C:/Program Files/nodejs/node.exe" "C:/Users/koolook/AppData/Roaming/npm/node_modules/mcporter/dist/cli.js" call TZZB.queryStockPosition --args '{"fundKey":"193735580","rzrqFundKey":"","manualId":""}' --no-oauth --output json` → `pipe_status=0`、stderr 空、完整 `holdings[8]`。

### 3.2 方案比较

| 维度 | 当前实现（17660 代理 + 屏幕 OCR） | TZZB MCP 换主源 |
|---|---|---|
| 持仓/交易字段 | 代理 `g_position.cbj` 含费；OCR 有列识别成本 | 账本字段完整，含清仓、资金流水、日/月盈亏、资产变动 |
| 启动依赖 | 17660 需要人工在 Python 策略编辑器跑一次；OCR 需要下单端窗口 | Node + mcporter + 用户登录态/账本同步 |
| 前台影响 | 代理/OCR 不抢前台，但 OCR 依赖窗口绘制 | 纯 HTTP/stdio，不抢前台 |
| 数据新鲜度 | 代理实时；OCR 受窗口状态影响 | 取决账本 `lastSyncTime`，可能滞后 |
| 失败模式 | 代理没起→OCR；OCR 失败→保留现有持仓 | MCP/Node/账本限流/登录态任一失效→需降级 |
| 改造风险 | 已 E2E PASS | 要改服务模型、错误语义、状态页、测试桩、部署脚本 |

### 3.3 结论与保留条件

结论：**MCP 更适合作为「持仓/交易/盈亏」的主数据源**，但 BOSS 本轮要的是可行性分析与「哪个方法更好」，不是直接换主源。因此：

- 当前代码撤回 `tzzb.py` / `from . import tzzb` / `query_positions_mcp()` 半实现，保持「17660 代理 → 屏幕 OCR」自洽；MCP 换主源仍 Proposed。
- 未来若要实施换源，按 §6.B 单独批准后再做；必须保留代理/OCR 降级或明确切换策略，并把 `lastSyncTime`、限流、登录态失效纳入状态页与错误处理。

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

理由：不改 BOSS 正在运行的源码级 custom 扩展 ID，避免运行中 TSP 和冻结目录行为突变；真包可以独立 inspect / install / update。代价：装包后不能与 custom 扩展同时启用，否则两个后台线程会同时写 `ths_sync_state.json` 与 `SelfStockCache.json`。必须在安装说明中写明二选一。

### 4.3 构建与校验证据

新增持久脚本：

- `tools/build_ths_sync_extension.py`：生成 `release/koolook.ths-sync-pack-1.0.0.zip`。
- `tools/verify_ths_sync_extension.py`：本地契约 + 运行期 `POST /api/import-center/inspect` 校验。

新鲜证据：

- `BUILD_OK out=C:\Users\koolook\ashare-tsp\release\koolook.ths-sync-pack-1.0.0.zip bytes=48344 files=11`，`[exit code: 0]`。
- `RESULT: VERIFY PASS zip=C:\Users\koolook\ashare-tsp\release\koolook.ths-sync-pack-1.0.0.zip bytes=48344 files=11 inspect=extension/koolook.ths-sync-pack errors=0`，`[exit code: 0]`。
- ZIP 含 11 文件：`extension.yaml`、`backend/__init__.py`、`backend/extension.py`、`backend/ths_sync/__init__.py`、`backend/ths_sync/reader.py`、`backend/ths_sync/routes.py`、`backend/ths_sync/service.py`、`backend/ths_sync/ths_agent.py`、`backend/ths_sync/writer.py`、`backend/ths_sync/xd_screen.py`、`frontend/extension.mjs`。
- `preview.kind == "extension"`、`preview.manifest.id == "koolook.ths-sync-pack"`、`preview.errors == []`。

产物放 `release/` 而非 `dist/`，因为 `.gitignore:16 dist/` 会忽略 ZIP；`release/` 当前未被忽略，便于作为 Release 资产交付。

## 5. Baseline Sync

- canonical owner：`backend/app/custom/ths_sync/` 是源码级实现；`tools/build_ths_sync_extension.py` 是包产物唯一构建入口；`docs/adr/0002-持仓数据源改用TZZB投资账本MCP.md` 是本决策记录。
- external install surface：包 ID `koolook.ths-sync-pack`，外置 API `/api/extensions/koolook.ths-sync-pack/`，前端 Page Pack route `/ths-sync-pack`。
- duplicate owners：`extensions/koolook.ths-sync/` 是历史迁移副本，不作为真包安装输入；`manifest.json` 为无效旧清单，应清理。
- baseline result：运行中的源码级 custom 扩展和真包不可同时启用；真包安装说明必须写二选一。

## 6. 后续动作

### 6.A 已完成

1. A/B/C、bug4 弹框、canonical 原生预览、板块入池、写回只加不减、零弹框、前端构建、冻结目录部署、重启、E2E 全绿。
2. MCP 可行性分析与方案对比。
3. 撤回 MCP 半实现，代码自洽。
4. 真 `tick-extension-package` ZIP 构建脚本、校验脚本、HTTP inspect 校验。
5. 运行目录 `D:/TSP/_internal` 已更新并验证。

### 6.B 仍 Proposed：MCP 换主源实施清单

1. 决定主源/降级顺序与 `lastSyncTime` 新鲜度阈值。
2. 新增 `tzzb_source.py`，返回 `list[reader.Position]` 与交易/盈亏摘要，禁止在 service 内拼 MCP 调用字符串。
3. 给 `build_status()` 增加 `positions_source` / `lastSyncTime` / `mcp_ok` / `mcp_error`。
4. 写 RED harness：MCP 成功、限流、登录态失效、持仓 0 行、字段转换、降级。
5. 用配置开关灰度，不删除 17660/OCR 前先双跑对照。
6. 更新前端状态页与扩展包构建脚本，重新 `inspect()`。
7. 由 BOSS 明确批准后再切换默认主源。

## 7. 风险与约束

- `git reset HEAD && git checkout -- .` 曾丢未提交成果；后续改动先 commit/stash。
- `queryDayCalendarSummary` 存在 `服务器繁忙` 限流；MCP 主源必须有降级与缓存。
- `SelfStockCache.json` 写回必须只加不减；禁止恢复 `ths_written_selfstock` 回环排除。
- `extension.yaml` 与 `plugin.yaml` 不得同时存在；包内不得有 `.pyc`/`__pycache__`。
- 真包与源码级 custom 扩展二选一启用，避免双线程写同一状态与自选缓存。
