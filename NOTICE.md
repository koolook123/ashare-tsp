# NOTICE

本项目是 [tick-stock-panel](https://github.com/shy3130/tick-stock-panel) 的二次开发分支（fork）。

- 上游项目：tick-stock-panel，作者/维护者 shy3130，许可证 MIT。
- 上游版权声明与许可全文见仓库根目录 `LICENSE`，本分支完整保留，未作修改。
- 派生版本标记：`v0.3.34-koolook.4`（基于 upstream `v0.3.34`）。

## 本分支新增内容（v0.3.34-koolook.4）

### 一、源码级后端扩展（进程内）

- `backend/app/custom/ths_sync/__init__.py`
- `backend/app/custom/ths_sync/service.py`
- `backend/app/custom/ths_sync/routes.py`

通过上游既有的后端扩展契约（`EXTENSION_ID` + `setup(registrar)` + `startup(context)`）挂载，
不改动 `backend/app/main.py` 与任何核心业务模块。对外路由前缀 `/api/custom/ths-sync`。

### 二、源码级前端扩展

- `frontend/src/custom/ths-sync/extension.tsx`
- `frontend/src/custom/ths-sync/Page.tsx`

通过上游既有的前端扩展契约（`import.meta.glob('../custom/*/extension.tsx')`）自动发现，
不改动核心路由与导航定义。侧栏入口「同花顺同步」，路由 `/ths-sync`。

### 三、外围同步桥（保留，v0.3.34-koolook.3 起提供）

- `tools/ths_tsp_sync.py`
- `tools/ths_tsp_sync.json.example`
- `tools/run_sync_once.bat`
- `tools/run_sync_loop.bat`

其能力已由上面的一、二两项在进程内实现，桥保留作为无扩展部署环境下的独立方案。

### 四、文档与合规

- `RELEASE_NOTES.md`
- `NOTICE.md`

新增部分随本项目同样以 MIT 许可提供。

## 使用范围声明

本分支仅用于个人本地盯盘：同花顺同步只读取本机同花顺客户端的自选股缓存文件，
以及本机 v1 服务（`http://127.0.0.1:17651/api/positions`）的持仓数据，写入本机 TSP 自选池。

- 不修改上游服务端业务逻辑，不改动核心 API 契约。
- 不重新分发上游桌面客户端二进制。
- 不含任何第三方凭据、账号、token 或运行时用户数据。
- 不做任何 UI 模拟点击、不抢前台焦点。

## 版本历史说明

- `v0.3.34-koolook.1` / `.2`：产物是未替换的官方 demo 空壳，不含同步功能，已废弃。
- `v0.3.34-koolook.3`：真实的同步桥 + 运行时页面插件（Page Pack）。
- `v0.3.34-koolook.4`：升级为源码级前后端扩展，进程内直写，无需独立桥进程。
