## v0.3.34-koolook.4（基于 upstream v0.3.34）

> 版本沿革：`.1` / `.2` 的产物是未替换的官方 demo 空壳（无同步功能，已废弃）；
> `.3` 为外围同步桥 + 运行时页面插件；**本版 `.4` 升级为源码级前后端扩展，进程内直写，不再需要独立桥进程。**

### 变更：从「外围桥 + 页面插件」升级为「源码级扩展」

上一版（`.3`）把同步逻辑放在仓库外的独立 Python 进程里，页面靠运行时上传的 Page Pack 注入。
本版改为走 TSP 上游自带的扩展契约，同步逻辑进入宿主进程，页面进入宿主构建产物：

| 位置 | 文件 | 挂载方式 |
| --- | --- | --- |
| 后端 | `backend/app/custom/ths_sync/{__init__,service,routes}.py` | `EXTENSION_ID` + `setup(registrar)` + `startup(context)`，前缀 `/api/custom/ths-sync` |
| 前端 | `frontend/src/custom/ths-sync/{extension,Page}.tsx` | `import.meta.glob('../custom/*/extension.tsx')` 自动发现，侧栏入口「同花顺同步」，路由 `/ths-sync` |

**未改动核心源码**：`backend/app/main.py`、`frontend/src/main.tsx`、`frontend/src/router*`、
`frontend/src/extensions/*`、`backend/app/services/watchlist.py` 一行未动。新增代码全部落在
上游专门为本项目的二开目录 `backend/app/custom/` 与 `frontend/src/custom/` 内，删目录即回退。

### 新增能力

- **进程内直写**：后端扩展直接调用 `backend/app/services/watchlist.py` 的 `add_batch` /
  `create_group` / `add_to_group` / `remove_from_group`，不再走 HTTP 回环。
- **自动同步线程**：`startup(context)` 里起 daemon 线程，默认 60 秒一轮（`MIN_INTERVAL=10`），
  随 TSP 启动即生效，无需另起进程、无需开机自启脚本。
- **页面**「同花顺同步」：`/ths-sync`。三张状态卡（同步引擎 / TSP 自选池 / 两池来源）+
  持仓明细表（含费成本、盈亏、盈亏%）+ 上次同步结果 + 同步机制说明 + 「刷新状态」「立即同步」按钮。
- **HTTP 接口**（都在 `/api/custom/ths-sync` 下）：
  - `GET /status`：完整状态（引擎、自选池、两池来源、上次结果）
  - `POST /sync`：立刻跑一轮
  - `GET /preview`：只读预览（不写库）
  - `GET /config` / `PUT /config`：`auto` / `interval` / `sync_selfstock` / `sync_positions` / `ths_user`
- **状态持久化**：`<data_dir>/ths_sync_state.json` 原子写，重启后沿用上次快照与配置。

### 两池口径（对齐原盯盘系统铁律）

| 分组 | 颜色 | 来源 | 用途 |
| --- | --- | --- | --- |
| 同花顺自选 | sky | `SelfStockCache.json` + 下单端 `selfstock_*.json` 并集 | 盯买点 |
| 同花顺持仓 | rose | v1 `/api/positions`（OCR 含费成本） | 盯卖点 |

只推增量、只移除自己写入且已不在两池的标的；用户手动加入的标的与自建分组不受影响。
同花顺缓存或 v1 服务暂时不可读时沿用上次快照，绝不误清空自选池。

### 使用（本机 Windows）

1. 启动 TSP → 左侧栏往下滚到扩展区 → 点「同花顺同步」。
2. 页面默认每 10 秒自动刷新状态；后台线程每 60 秒自动同步一轮。
3. 需要立刻同步时点「立即同步」。
4. 旧方案（`.3` 的外围桥）已停用：`127.0.0.1:17652` 桥进程已杀、开机自启 `ths_tsp_sync.vbs` 已删、
   `/api/ext-pages` 已清空。`tools/ths_tsp_sync.py` 保留在仓库里，作为无扩展部署环境的备用方案。

### 验证记录（2026-11-11 收盘后，冻结客户端 v0.3.34 实测）

- `tsc -b`：0 错误；`vite build`：17.57s 通过，新增 chunk `extension-DiQ3okqr.js` + `Page-D8AL8ixs.js`
- 部署到冻结客户端：`backend/app/custom/ths_sync` → `D:/TSP/_internal/app/custom/ths_sync`；
  `frontend/dist` → `D:/TSP/_internal/static`（旧目录已备份 `static.bak_before_ths_sync`）→ 重启生效
- `GET /api/custom/ths-sync/preview`：`selfstock_ok=true` 86 只、`positions_ok=true` 7 只
- `POST /api/custom/ths-sync/sync`：`ok=true`、`errors=[]`、`elapsed_ms=49`、`selfstock=86`、`positions=7`
- `GET /api/custom/ths-sync/status`：`bridge{version:3, mode:"in-process", auto:true, interval:60, thread_alive:true}`、
  `tsp.watchlist_count=86`、两组 `同花顺自选/同花顺持仓`、`position_pnl_total=518.5`
- 自选池归属：`total=86`、`owned_by_ths=86`、`group_ids` 长度分布 `{1: 79, 2: 7}`（79 只仅自选组，7 只同时在两池）
- 页面（Playwright + 本机 Edge 实测）：侧栏 `同花顺同步` 入口存在；`/ths-sync` 全部标记文案渲染；
  持仓表 7 行；点「立即同步」出 toast「同步完成 · 新增 0 / 移除 0」；无 console error、无网络失败
- 修复：`fmtPct` 入参是小数（内部 ×100），而 v1 的 `pnl_pct` 已是百分数，原样传入会放大 100 倍；
  已改为 `fmtPct(pnl_pct / 100)`，显示恢复为 `-0.69%` / `+2.65%`

### 兼容性说明

- 冻结桌面客户端（PyInstaller onedir）的扩展加载器按磁盘目录扫描：`app.custom` 用
  `pkgutil.iter_modules` 扫 `_internal/app/custom/`，前端静态资源读 `_internal/static`
  （`backend/app/config.py:243`）。因此**把扩展文件拷进这两个目录并重启即可生效，无需重新打包 exe**。
- 需要随 exe 一起分发的场景，用上游 `packaging/tsp.spec` 重新构建即可，扩展目录会被一并收进产物。

### 合规

- MIT 许可证，上游 `LICENSE` 完整保留；派生说明见 `NOTICE.md`
- 仓库不含 token、账号、SMTP 授权码、运行时数据库与自选股缓存
