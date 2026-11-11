# NOTICE

本项目是 [tick-stock-panel](https://github.com/shy3130/tick-stock-panel) 的二次开发分支（fork）。

- 上游项目：tick-stock-panel，作者/维护者 shy3130，许可证 MIT。
- 上游版权声明与许可全文见仓库根目录 `LICENSE`，本分支完整保留，未作修改。
- 派生版本标记：`v0.3.34-koolook.6`（基于 upstream `v0.3.34`）。

## 本分支新增内容（v0.3.34-koolook.6）

### 一、源码级后端扩展（进程内，直连同花顺远航版本机安装）

- `backend/app/custom/ths_sync/__init__.py`：扩展入口，`EXTENSION_ID = "koolook.ths-sync"`
- `backend/app/custom/ths_sync/reader.py`：定位同花顺用户目录、读自选缓存、读板块成分、经 `127.0.0.1:17660` 读持仓
- `backend/app/custom/ths_sync/writer.py`：局部替换写板块（保留 `<hevo>` 私有属性）、原子写、写前备份、写后回读校验、写自选缓存、`ths_running()` 客户端运行探测
- `backend/app/custom/ths_sync/service.py`：双向同步引擎 + 待写入队列 + 写自选主通道 + `with_positions` 三态 + 后台守护线程
- `backend/app/custom/ths_sync/routes.py`：`/api/custom/ths-sync` 下的 `status` / `sync` / `sync/dry` / `sync/positions` / `config` / `preview` / `blocks` / `agent`
- `backend/app/custom/ths_sync/ths_agent.py`：跑在同花顺内置 Python 3.5 + IronPython 环境里的只读代理（`from ths_api import *` 取 `g_position` / `g_money`）

通过上游既有的后端扩展契约（`EXTENSION_ID` + `EXTENSION_API_VERSION` + `setup(registrar)` + 可选 `startup(context)`）挂载，
不改动 `backend/app/main.py`、`backend/app/services/watchlist.py` 或任何核心业务模块。

### 二、源码级前端扩展

- `frontend/src/custom/ths-sync/extension.tsx`
- `frontend/src/custom/ths-sync/Page.tsx`

通过上游既有的前端扩展契约（`import.meta.glob('../custom/*/extension.tsx')`）自动发现，
不改动核心路由与导航定义。侧栏入口「同花顺同步」，路由 `/ths-sync`。

### 三、外围同步桥（历史版本遗留，保留但已停用）

- `tools/ths_tsp_sync.py`
- `tools/ths_tsp_sync.json.example`
- `tools/run_sync_once.bat`
- `tools/run_sync_loop.bat`

`.3` 版本的外围方案（独立桥进程 + 运行时页面插件）。`.4` 起能力已由上面的一、二两项在进程内实现，
`.5` 起不再读取 v1 A 股助手，桥仅作为无扩展部署环境下的参考实现保留。

### 四、文档与合规

- `RELEASE_NOTES.md`
- `NOTICE.md`

## 本分支不含

账号、密码、Token、Cookie、持仓数据、同花顺用户目录路径的硬编码备份。同花顺安装根目录通过环境变量
`THS_ROOT` 覆盖，代码内默认值仅为本机常见安装位置。

## v0.3.34-koolook.6 变更摘要

- 持仓同步由自动轮询改为**手动一键**（`POST /api/custom/ths-sync/sync/positions`）；自动线程固定
  `run_once(with_positions=False)`，不再触碰持仓通道。
- TSP → 同花顺 的写入主通道改为**自选缓存** `SelfStockCache.json`（`write_ths_selfstock` 默认开启）；
  写板块 `sync_tsp_to_ths` 默认关闭，仅作为可选实验开关保留。
- 页面新增可开关的「自动同步」大开关，另有三个方向开关。
- 修复 `.5` 中「写入集合把自己算进去、下一轮又被整体排除」导致一次同步清空两边数据的级联 bug；
  写自选改为只做加法，并新增「来源读取失败即跳过」的第三条硬保护。
- `ths_sync_state.json` 引入 `state_version`，`v1 → v2` 一次性迁移上述默认值。
