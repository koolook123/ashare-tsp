## v0.3.34-koolook.6（基于 upstream v0.3.34）

> 版本沿革：`.1`/`.2` 是未替换的官方 demo 空壳（无同步功能，已废弃）；`.3` 为外围同步桥 + 运行时页面插件；
> `.4` 升级为源码级前后端扩展，但持仓仍读 v1 A 股助手；`.5` 彻底脱离 v1、直读同花顺本体；
> **本版 `.6` 按实际可用通道重划主次：持仓改手动一键，TSP → 同花顺 改写自选（主通道），
> 并在页面加上可开关的自动同步；同时修掉一个会一次吃掉两边数据的级联 bug。**

### 一句话变化

`.6` 把「哪个方向自动、哪个方向手动、写哪个文件」按实测结论定死：

- **同花顺自选 → TSP**：自动，跟随页面上的「自动同步」开关，每 60 秒一轮。
- **同花顺持仓 → TSP**：**手动一键**（右上角「一键同步持仓」），自动轮询不再碰持仓。
- **TSP → 同花顺自选**：主通道，默认开启，写 `SelfStockCache.json`，重启同花顺后读入。
- **TSP → 同花顺板块 `ai选股`**：默认关闭（写不进去，只作实验开关保留）。

### 为什么这么改（三条实测结论，不是推测）

| 通道 | 实现 | 实测结论 |
| --- | --- | --- |
| 同花顺自选 → TSP | 读 `bin/users/<user>/SelfStockCache.json` | 稳定可读，40 只自选 |
| 同花顺持仓 → TSP | 同花顺进程内代理 `127.0.0.1:17660`（`ths_agent.py`） | 磁盘上**没有任何**持仓快照，只能进程内取 `g_position` |
| TSP → 同花顺板块 | 写 `blockstockV3.xml` 的 `ai选股` 块 | 运行期写入被内存态覆盖（实测 104 秒被刷回）；新增块启动时被丢弃；`ai选股` 本身是服务端托管块 |
| TSP → 同花顺自选 | 写 `SelfStockCache.json` | 文件运行期不被覆写，写入可长期存活，重启客户端读入 |

1. **持仓为什么改手动**：读持仓要连 `127.0.0.1:17660`，代理没起时会走满 socket 超时。自动线程每 60 秒
   跑一轮，不该被一个可能永远不通的连接拖慢。改成手动后，自动线程只做「读一个 json + 写一个 json」，
   单轮 50 毫秒级。
2. **为什么不写板块改写自选**：写板块没有可用窗口——运行期 2 分钟内被内存态刷回，新增块启动被丢弃，
   `ai选股` 本身还是服务端托管块，本地只是镜像。写自选则相反：文件不被覆写，写进去就能活到下次启动。
3. **页面上为什么要有自动同步开关**：`PUT /api/custom/ths-sync/config {"auto": false}` 即可停掉后台线程的
   轮询（线程仍在、不空转业务），开关状态落盘在 `ths_sync_state.json`，重启后保持。

### 新增：手动一键持仓同步

- 后端新增 `POST /api/custom/ths-sync/sync/positions` → `service.run_once(with_positions=True)`。
- `run_once` 签名增加 `with_positions: bool | None = None`：`None` 按配置；`False` 强制不读（自动线程走这条）；
  `True` 强制读（手动按钮走这条）。
- 自动线程固定调 `run_once(with_positions=False)`，注释写明「自动轮询只跑自选、不碰持仓」。
- 配置项 `sync_positions` 默认 `false`；页面上它不再是复选框，改成一行状态文字 + 「立即同步持仓」小按钮。

### 新增：自动同步开关

- 页面「同步开关」卡顶部独立大号开关，直接 `PUT /config {"auto": ...}`，关闭后 hint 提示「关掉后只在点按钮时同步」。
- 另有三个普通复选项：`sync_selfstock`（同花顺自选 → TSP，自动）、`write_ths_selfstock`
  （TSP → 同花顺自选，主通道，需重启）、`sync_tsp_to_ths`（TSP → 同花顺板块，默认关闭）。

### ★ 修复：一次同步吃掉两边数据的级联 bug

`.5` 里写自选成功后把「整个目标集」记进 `ths_written_selfstock`，下一轮又把它整体从入向来源里剔除，
于是 `desired_self` 变空集 → `owned - 空集` 把 TSP 里 40 只同步标的全删 → 写回又把同花顺自选截成 5 只。
**一次同步把两边的数据都吃掉了一大半。**

修法三处：

1. 删掉 `written_th` 排除逻辑，`self_codes_native = self_codes`（入向就是同花顺有什么就同步什么，只增不减才安全）。
2. 写自选的目标集基准改为**来源本身**：`sorted({c for c in (self_codes or []) if is_a_stock(c)} | {c for c in native_codes if c})`，
   不再用「TSP 分组反查」（分组是下游状态，上游算错它也错）。
3. 新增第三条硬保护：本轮没读到同花顺自选（`self_codes is None`）时跳过写自选缓存。

代价写清楚：现在在 TSP 里删一只**不会**让同花顺少一只——删除请直接在同花顺里删。这个取舍是为了绝不可能误删。

### 状态文件迁移（v1 → v2）

`ths_sync_state.json` 增加 `state_version`。读到 `state_version < 2` 时一次性迁移：
`sync_positions=False`、`sync_tsp_to_ths=False`、`write_ths_selfstock=True`、清空 `pending_block*`，
然后写回版本号。只迁一次，迁移后用户的手动改动不再被覆盖。

### 新增/重写文件

| 位置 | 文件 | 说明 |
| --- | --- | --- |
| 后端 | `backend/app/custom/ths_sync/reader.py` | 定位用户目录、读自选缓存、读板块成分、走 17660 读持仓 |
| 后端 | `backend/app/custom/ths_sync/writer.py` | 局部替换写板块（保留 `<hevo>` 私有属性）、原子写、写前备份、写后回读校验、写自选缓存 |
| 后端 | `backend/app/custom/ths_sync/ths_agent.py` | 跑在同花顺内置 Python 3.5 + IronPython 里的只读代理，`from ths_api import *` 拿 `g_position` |
| 后端 | `backend/app/custom/ths_sync/service.py` | 双向同步引擎 + 待写入队列 + 写自选主通道 + 三态 `with_positions` + 后台线程 |
| 后端 | `backend/app/custom/ths_sync/routes.py` | `/status` `/sync` `/sync/dry` `/sync/positions` `/config` `/preview` `/blocks` `/agent` |
| 前端 | `frontend/src/custom/ths-sync/Page.tsx` | 三张状态卡 + 四条通道边界 + 自动同步开关 + 一键同步持仓 + 持仓明细（9 列含市值/盈亏%） |

### 未改动核心源码

`backend/app/main.py`、`backend/app/services/watchlist.py`、`frontend/src/main.tsx`、
`frontend/src/router*`、`frontend/src/extensions/*` 一行未动。全部新增代码落在上游专门为二开准备的
`backend/app/custom/` 与 `frontend/src/custom/` 目录内，删目录即回退。

### 部署方式（冻结客户端，无需重打包）

TSP 桌面端是 PyInstaller onedir，扩展目录在磁盘上、启动时被扫描，因此：

```bash
# 后端
cp -f backend/app/custom/ths_sync/*.py "D:/TSP/_internal/app/custom/ths_sync/"
rm -rf "D:/TSP/_internal/app/custom/ths_sync/__pycache__"
# 前端
cd frontend && node node_modules/vite/bin/vite.js build
rm -rf "D:/TSP/_internal/static" && cp -r dist "D:/TSP/_internal/static"
# 重启
powershell -NoProfile -Command "Get-Process TSP | Stop-Process -Force; Start-Process 'D:\\TSP\\TSP.exe'"
```

侧栏「同花顺同步」→ 路由 `/ths-sync`（侧栏列表较长，需往下滚）。

### 使用前必须做的一步（持仓通道）

同花顺从不把持仓落盘，只能由进程内代理提供。在远航版里打开「我的策略」，
运行一次 `backend/app/custom/ths_sync/ths_agent.py`（或同目录的 `agent_ths.py`），
它会在 `127.0.0.1:17660` 常驻。代理未运行时页面显示「持仓代理 未运行」，
「一键同步持仓」会沿用上次快照、绝不清空 TSP 自选池。

### 实测记录（2026-11-11）

- 自选入向：同花顺 40 只 → TSP 自选池 40 只，分组「同花顺自选」40 /「同花顺持仓」5。
- 自动同步稳定性：`sync_count` 递增，`added=0 removed=0`，两边计数恒为 40 / 40，无抖动。
- 写自选出向：TSP 加探针 `600000` → `POST /sync` 返回
  `selfstock_write={ok:true,changed:true,before:40,after:41,need_restart:true}`，
  同花顺缓存实测 41 只、含 `600000` → **主通道确认可用**；探针已清、缓存已回滚到 40 只。
- 自动同步开关：`PUT /config {"auto":false}` → `false`，再 `{"auto":true}` → `true`，后台线程始终 `alive=true`。
- 手动持仓：`POST /sync/positions` → `ok=true`，因代理未起 `positions=0`，本轮跳过且不清空。
- 级联 bug 现场已恢复：同花顺 `SelfStockCache.json` 从 40 只被写坏成 5 只后，用 `writer` 的
  `.bak_ths_sync` 备份整份回滚到 40 只；TSP 侧 `POST /sync` 补回 35 只；毒状态
  `ths_written_selfstock` 已清空。
- 前端：`tsc -b` 0 错误；`vite build` 9.65s；Playwright 实测 `/ths-sync` 14 个关键文案全存在
  （含「一键同步持仓」「立即同步自选」「四条通道的真实边界」「自动同步」「待写入板块」「需重启」），
  侧栏 `a[href="/ths-sync"]` 存在，点击「立即同步自选」toast 显示「同步完成 · 新增 0 / 移除 0 / 写自选 40→40」，
  无 pageerror。

### 已知边界（写在这里，避免以后误判）

1. `ai选股` 是**服务端托管块**：某次启动它从 19 只变 12 只，不是本地改的，是客户端从云端拉的新内容。
   本地文件只是镜像，云端同步仍可能整块覆盖本地写入。
2. 新增一个客户端内存里没有的块，启动时会被丢弃（客户端按自己的块清单重建文件，带 `IsRemove` 标记）。
   所以本版只改**已存在**的块，不做「新建板块」。
3. 同花顺自选缓存有两份且各自独立：`bin/users/<user>/SelfStockCache.json` 与
   `transaction/xiadan-plus/selfstock_*.json`。本版只操作前者。
4. `TSP → 同花顺自选` 现在只做加法：在 TSP 删一只不会让同花顺少一只，删除请直接在同花顺里删。
5. 写自选后必须**重启同花顺**才被读入；写板块必须在**同花顺关闭期间**写入才会在下次启动被读入。

### 合规

- 上游 tick-stock-panel，作者 shy3130，MIT。`LICENSE` 完整保留，未修改。
- 派生版本标记见 `NOTICE.md`。
- 本分支不含任何账号、密码、Token、持仓数据；同花顺路径通过环境变量 `THS_ROOT` 可覆盖，默认值为本机安装路径。
