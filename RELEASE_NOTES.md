## v0.3.34-koolook.5（基于 upstream v0.3.34）

> 版本沿革：`.1`/`.2` 是未替换的官方 demo 空壳（无同步功能，已废弃）；`.3` 为外围同步桥 + 运行时页面插件；
> `.4` 升级为源码级前后端扩展，但持仓仍读 v1 A 股助手；**本版 `.5` 彻底脱离 v1，改为直读同花顺本体，
> 并把「写回同花顺」的真实通道按实测结论重做。**

### 一句话变化

`.5` 之后，本扩展与 v1（`ashare-copilot`）**没有任何关系**：不调 v1 的 HTTP 接口、不读 v1 的数据文件、
不依赖 v1 进程是否在跑。数据源与写入目标全部指向同花顺远航版本机安装。

### 数据通道（全部为实测结论，不是推测）

| 通道 | 实现 | 实测结论 |
| --- | --- | --- |
| 同花顺自选 → TSP | 读 `bin/users/<user>/SelfStockCache.json` | 稳定可读，40 只自选 |
| 同花顺持仓 → TSP | 同花顺进程内代理 `127.0.0.1:17660`（`ths_agent.py`） | 磁盘上**没有任何**持仓快照；只能进程内取 `g_position` |
| TSP → 同花顺板块 | 写 `bin/users/<user>/blockstockV3.xml` 的 `ai选股` 块 | 运行期写入被内存态覆盖（实测 104 秒被刷回），**只有关闭期间写入才在下次启动被读入** |
| TSP → 同花顺自选 | 写 `SelfStockCache.json` | 文件不被覆写，但客户端运行中不重读，需重启才生效 |

### 新增：待写入队列（解决「写了又消失」）

同花顺运行期间写 `blockstockV3.xml` 会在数分钟内被客户端的**内存状态**整份覆盖，客户端从不读回。
因此本版不再在运行期写文件：

1. 检测到同花顺进程在运行 → 把目标板块内容存进 `ths_sync_state.json` 的 `pending_block` 队列，
   `run_once` 的 `skipped` 里明确写出「已排队 N 只，关闭同花顺后自动落盘」；
2. 同花顺关闭后（任何一轮同步或手动点「立即同步」）→ 队列自动落盘，写入内容会在下次启动被读入；
3. 页面新增「待写入板块」指标与「同花顺客户端」运行状态，用户能直接看到「排队中 / 已落盘」。

`block_info` 增加 `queued`、`need_restart` 字段；`status.ths` 增加 `block_volatile`（客户端是否在运行）
与 `block_volatile_note`（当前该走哪条路径的人话说明）；`status` 新增 `pending` 段。

### 新增/重写文件

| 位置 | 文件 | 说明 |
| --- | --- | --- |
| 后端 | `backend/app/custom/ths_sync/reader.py` | 新建：定位用户目录、读自选缓存、读板块成分、走 17660 读持仓 |
| 后端 | `backend/app/custom/ths_sync/writer.py` | 新建：局部替换写板块（保留 `<hevo>` 私有属性）、原子写、写前备份、写后回读校验、写自选缓存 |
| 后端 | `backend/app/custom/ths_sync/ths_agent.py` | 新建：跑在同花顺内置 Python 3.5 + IronPython 里的只读代理，`from ths_api import *` 拿 `g_position` |
| 后端 | `backend/app/custom/ths_sync/service.py` | 重写：双向同步引擎 + 待写入队列 + 后台线程 |
| 后端 | `backend/app/custom/ths_sync/routes.py` | 重写：`/status` `/sync` `/sync/dry` `/config` `/preview` `/blocks` `/agent` |
| 前端 | `frontend/src/custom/ths-sync/Page.tsx` | 重写：三张状态卡 + 待写入队列 + 持仓明细（9 列含市值/盈亏%） + 通道边界说明 + 同步开关 |

### 未改动核心源码

`backend/app/main.py`、`backend/app/services/watchlist.py`、`frontend/src/main.tsx`、
`frontend/src/router*`、`frontend/src/extensions/*` 一行未动。全部新增代码落在上游专门为二开准备的
`backend/app/custom/` 与 `frontend/src/custom/` 目录内，删目录即回退。

### 部署方式（冻结客户端，无需重打包）

TSP 桌面端是 PyInstaller onedir，扩展目录在磁盘上、启动时被扫描，因此：

```bash
# 后端
cp -f backend/app/custom/ths_sync/*.py "D:/TSP/_internal/app/custom/ths_sync/"
# 前端
cd frontend && node node_modules/vite/bin/vite.js build
rm -rf "D:/TSP/_internal/static" && cp -r dist/. "D:/TSP/_internal/static/"
# 重启
powershell -NoProfile -Command "Get-Process TSP | Stop-Process -Force; Start-Process 'D:\TSP\TSP.exe'"
```

侧栏「同花顺同步」→ 路由 `/ths-sync`。

### 使用前必须做的一步（持仓通道）

同花顺从不把持仓落盘，只能由进程内代理提供。在远航版里打开「我的策略」，
运行一次 `backend/app/custom/ths_sync/ths_agent.py`（或同目录的 `agent_ths.py`），
它会在 `127.0.0.1:17660` 常驻。代理未运行时页面显示「持仓代理 未运行」，
同步会沿用上次快照、绝不清空 TSP 自选池。

### 实测记录（2026-11-11）

- 自选：同花顺 40 只 → TSP 自选池 40 只，分组「同花顺自选」40 /「同花顺持仓」5，`native_count=0`。
- 板块读取：`ai选股` 12 只，回读一致。
- 运行期排队：加入 2 只自选 → `POST /sync` 返回 `block.queued=true`、`pending.block=2`、`skipped` 明确提示。
- 关闭同花顺后再同步：`block.queued=false`、`pending.block=0`、`block_count` 从 12 → 2，落盘成功。
- 恢复现场：清掉探针、写回 `ai选股` 12 只，重启同花顺后文件仍为 12 只（启动读入生效）。
- 前端：`tsc -b` 0 错误；`vite build` 15.38s；Playwright 实测 `/ths-sync` 14 个关键文案全存在、
  侧栏入口存在、5 个开关状态正确、点击「立即同步」无 pageerror。

### 已知边界（写在这里，避免以后误判）

1. `ai选股` 是**服务端托管块**：某次启动它从 19 只变 12 只，不是本地改的，是客户端从云端拉的新内容。
   本地文件只是镜像，云端同步仍可能整块覆盖本地写入。
2. 新增一个客户端内存里没有的块，启动时会被丢弃（客户端按自己的块清单重建文件，带 `IsRemove` 标记）。
   所以本版只改**已存在**的块，不做「新建板块」。
3. 同花顺自选缓存有两份且各自独立：`bin/users/<user>/SelfStockCache.json` 与
   `transaction/xiadan-plus/selfstock_*.json`。本版只操作前者。
4. `TSP → 同花顺自选` 默认关闭：该文件同时是入向数据源，写回会形成回环并覆盖手工维护的自选。

### 合规

- 上游 tick-stock-panel，作者 shy3130，MIT。`LICENSE` 完整保留，未修改。
- 派生版本标记见 `NOTICE.md`。
- 本分支不含任何账号、密码、Token、持仓数据；同花顺路径通过环境变量 `THS_ROOT` 可覆盖，默认值为本机安装路径。
