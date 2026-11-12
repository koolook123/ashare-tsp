## v0.3.34-koolook.7（基于 upstream v0.3.34）

> 版本沿革：`.1`/`.2` 是未替换的官方 demo 空壳（无同步功能，已废弃）；`.3` 为外围同步桥 + 运行时页面插件；
> `.4` 升级为源码级前后端扩展，但持仓仍读 v1 A 股助手；`.5` 彻底脱离 v1、直读同花顺本体；
> `.6` 按实测结论重划主次（持仓手动一键、TSP → 同花顺改写自选、页面加自动同步开关）；
> **本版 `.7` 让持仓彻底不需要人工运行脚本：新增「后台截屏 + 逐列 OCR」第二通道，
> 并在部署验证中修掉一个把持仓池污染成自选池副本的分组 bug。**

### 一句话变化

- **持仓读取有两条通道**：首选同花顺进程内代理（`127.0.0.1:17660`，`g_position` 含费成本）；
  代理没起时**自动降级**为后台截屏识别下单端持仓表（`BitBlt` + 逐列裁条 OCR），
  **不抢前台、不模拟鼠标、不需要在同花顺里跑任何脚本**。
- 页面上「持仓代理」指标随通道变成「持仓（进程内）」或「持仓（屏幕识别）」，
  新增 `positions_channel` 字段（`agent` / `screen`）贯穿 `/status` 与 `/sync/positions` 的返回。
- 修掉分组污染：持仓池不会再被塞进自选池的全部成员。

### 新增：`xd_screen.py`（后台读下单端持仓）

同花顺从不把持仓落盘（`strategy.dat` 的四张 `pysignal_*` 表恒为空、`PositionBlock.xml` 只是面板布局、
`BSData/*` 是残缺成交流水），精确持仓只在进程内 `g_position`。`.6` 之前唯一取法是人工在内置 Python
里跑一次 `ths_agent.py` 起 17660 代理——脚本界面藏在下单端「Python策略编辑器」里，且客户端重启就断。

`.7` 加了第二条路：**下单端窗口本身就把持仓表画在屏幕上**，把这块像素读出来即可，不需要脚本、不需要焦点。

实现要点（全部踩过坑，实证有效）：

1. **抓图**：`PrintWindow` 对 DirectUI 自绘窗口三种 flag 全返回纯黑（实测 `nonzero=0`），
   必须回退 `BitBlt(mdc, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)`。`capture()` 先试 `PrintWindow(hwnd, mdc, 2)`，
   用「每 4096 字节抽样、≥3 个非黑像素」判定，全黑自动回退，仍全黑才抛错。
2. **兼容位图必须用窗口 DC**：`CreateCompatibleBitmap(mdc, w, h)`（内存 DC 的默认位图是 1×1 单色）
   会得到单色位图，`PrintWindow` 返回 1 但读回纯黑。改成 `CreateCompatibleBitmap(hdc, w, h)` 才出图。
3. **DPI 感知**：必须先 `SetProcessDpiAwareness(2)`，否则 200% 缩放下 `GetWindowRect` 只返回逻辑尺寸，
   持仓表根本不在图里。
4. **OCR 分列**：整图 `--psm 6` 必丢小数点（`2.50` → `250`、`-1.15` → `-115`）。
   改为**按列竖直裁条**，每列单独跑
   `tesseract -l eng --psm 6 -c tessedit_char_whitelist=0123456789.- tsv`，四周补 16px 白边，小数点全部救回。
5. **列心靠右边界聚类**：数字列右对齐，比固定像素稳。只统计 `0.13W < x < 0.66W` 且像数字的 token，
   容差 14px。裁剪边界是 `x0 = 左邻列心 + 20`、`x1 = 本列心 + 8`（用「相邻列心中点」作边界会把左列切半，
   `+40` 又会把左邻列的小数点吸进来）。
6. **资金区**：顶部两行 5 + 4 个指标，按 y 容差 20px 聚类后只认「一行至少 4 个数」的行
   （同行数字 top 抖动 1~3px 会被精确去重拆成多行）；另加 `x < 0.62W` 过滤，挡掉右侧一个假 `0`。
7. **股票名**：从 `bin/stockname/stocknameV2.db` 的表 `tablestock(CODE, NAME, ...)` 查，
   库里是「七 匹 狼|七 匹 狼@f」带内嵌空格，要压平。

实测（本机 2918×1173，专业版下单窗口）：抓图 0.16s、整图定位 0.85s、四行 9 列全部与界面逐项一致，
资金区 `cash 241.44 / takeout 0.00 / market_value 11524.00 / day_pnl 184.44 / pos_ratio 93.1% /
avail 0.00 / asset 12379.29 / pos_pnl 354.27 / day_pct 1.51%`，端到端 `snapshot()` 1.7s。

### 双通道与缓存

- `service.read_positions_any()`：先 `reader.read_positions()`（17660 代理，返回 `(positions, "agent")`），
  不可用才 `_positions_from_screen()`（返回 `(out, "screen")`）。
- `_SCREEN_CACHE` + `_SCREEN_TTL = 4.0`：前端 10 秒轮询，缓存避免每轮都拉起 tesseract。
- `positions_ok` 从「代理是否在线」改为「本轮是否真读到持仓」，所以代理不在也能显示 4 只。

### 新增接口字段

| 位置 | 字段 | 说明 |
| --- | --- | --- |
| `GET /status` → `ths` | `positions_channel` | `agent` / `screen` / 空串（两路都读不到） |
| `POST /sync/positions` | `positions_channel` | 同上，标明本轮用哪条通道 |

### ★ 修复：持仓池被污染成自选池副本

部署后 `/status` 出现 `同花顺自选 44 / 同花顺持仓 44` —— 持仓组里塞进了全部自选。

根因：`keep` 是一个集合、两路混装。`positions is None` 时它装的是「同花顺自选」分组成员（44 只），
而 `_sync_group_members()` 的语义是**让该分组成员正好等于 desired**，于是
`_sync_group_members(group_pos_id, desired_pos | keep, ...)` 把 44 只全写进了持仓组。

修法：拆成 `keep_self` / `keep_pos`，两处对齐各用各的：
`_sync_group_members(group_self_id, desired_self | keep_self, ...)` /
`_sync_group_members(group_pos_id, desired_pos | keep_pos, ...)`。
修后 `同花顺自选 42 / 同花顺持仓 4`，持仓组只有 601606 / 600410 / 002029 / 002529。

> 教训写在代码注释里：强制对齐语义的分组，desired 里混进别的池子会立刻污染该池。

### 新增/重写文件

| 位置 | 文件 | 说明 |
| --- | --- | --- |
| 后端 | `backend/app/custom/ths_sync/xd_screen.py` | **本版新增**：后台 BitBlt 截屏 + 逐列裁条 OCR，读下单端持仓表与资金区 |
| 后端 | `backend/app/custom/ths_sync/reader.py` | 定位用户目录、读自选缓存、读板块成分、走 17660 读持仓 |
| 后端 | `backend/app/custom/ths_sync/writer.py` | 局部替换写板块（保留 `<hevo>` 私有属性）、原子写、写前备份、写后回读校验、写自选缓存 |
| 后端 | `backend/app/custom/ths_sync/ths_agent.py` | 跑在同花顺内置 Python 3.5 + IronPython 里的只读代理（可选通道） |
| 后端 | `backend/app/custom/ths_sync/service.py` | 双向同步引擎 + 待写入队列 + 写自选主通道 + 持仓双通道 + 三态 `with_positions` + 后台线程 |
| 后端 | `backend/app/custom/ths_sync/routes.py` | `/status` `/sync` `/sync/dry` `/sync/positions` `/config` `/preview` `/blocks` `/agent` |
| 前端 | `frontend/src/custom/ths-sync/Page.tsx` | 三张状态卡 + 四条通道边界 + 自动同步开关 + 一键同步持仓 + 持仓明细 + 通道标签 |

### 未改动核心源码

`backend/app/main.py`、`backend/app/services/watchlist.py`、`frontend/src/main.tsx`、
`frontend/src/router*`、`frontend/src/extensions/*` 一行未动。全部新增代码落在上游专门为二开准备的
`backend/app/custom/` 与 `frontend/src/custom/` 目录内，删目录即回退。

### 部署方式（冻结客户端，无需重打包）

TSP 桌面端是 PyInstaller onedir，扩展目录在磁盘上、启动时被扫描，因此：

```bash
# 后端
rm -rf "D:/TSP/_internal/app/custom/ths_sync/__pycache__"
cp -f backend/app/custom/ths_sync/*.py "D:/TSP/_internal/app/custom/ths_sync/"
# 前端
cd frontend && node node_modules/typescript/bin/tsc -b && node node_modules/vite/bin/vite.js build
rm -rf "D:/TSP/_internal/static" && cp -r dist "D:/TSP/_internal/static"
# 重启
python -c "import subprocess;subprocess.Popen([r'D:\TSP\TSP.exe'],creationflags=0x00000008|0x00000200)"
```

> Windows 上 `powershell Start-Process` 与 `cmd /c start` 在部分会话里会静默失败（进程表为空、无报错），
> `subprocess.Popen` + `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP` 最可靠。

侧栏「同花顺同步」→ 路由 `/ths-sync`（侧栏列表较长，需往下滚）。

### 持仓通道（本版已不需要额外步骤）

- **开箱即用**：同花顺下单端开着、停在**持仓**页即可，后台截屏识别直接读。
- **可选更快**：在远航版「我的策略」里运行一次 `ths_agent.py` 起 17660 代理，读的是 `g_position` 原始字段
  （成本 `cbj` 含费），比 OCR 更快更准；代理没起会自动降级，不会报错。
- 两路都读不到时（下单端没开 / 窗口被完全遮挡）才沿用上次快照，**绝不清空** TSP 自选池。

### 实测记录（2026-11-12）

- 屏幕通道：`rows 4`，`600410 200/13.033/13.000/-6.53/-0.25`、`601606 100/31.825/32.620/+79.47/+2.50`、
  `002029 300/9.322/9.460/+41.50/+1.48`、`002529 400/7.142/7.060/-32.66/-1.15`，与下单端界面逐项一致；
  资金区 9 项全对；`snapshot()` 1.7s。
- `/status`：`agent_ok=false / positions_ok=true / positions_channel=screen / positions_count=4 /
  selfstock=42 / block=12`，5.3s（屏幕 OCR 拖慢，可接受）。
- `POST /sync/positions`（4.5s）→ `positions 4, positions_channel screen, added [], removed [], errors []`。
- 分组污染修复后：`同花顺自选 42 / 同花顺持仓 4`，持仓组仅 4 只。
- 自动同步稳定性三轮：`sync_count 0→1→2`、`last_sync 18:39:00 → 18:40:01`、`thread_alive true`；
  第一轮 `removed 2`（清掉已不在同花顺自选的 600135 / 600815），之后 `added 0 removed 0`、
  `selfstock_write.changed=false`，计数稳定 42 / 42。
- 前端：`tsc -b` 0 错误；`vite build` 9.02s；Playwright（msedge headless，1500×1100）
  11 个关键文案全存在（含「持仓（屏幕识别）」「屏幕识别」「一键同步持仓」「立即同步自选」），
  侧栏 `a[href="/ths-sync"]` 存在，`ERRORS [] FAILS []`。

### 已知边界（写在这里，避免以后误判）

1. `ai选股` 是**服务端托管块**：某次启动它从 19 只变 12 只，不是本地改的，是客户端从云端拉的新内容。
   本地文件只是镜像，云端同步仍可能整块覆盖本地写入。
2. 新增一个客户端内存里没有的块，启动时会被丢弃（客户端按自己的块清单重建文件，带 `IsRemove` 标记）。
   所以本版只改**已存在**的块，不做「新建板块」。
3. 同花顺自选缓存有两份且各自独立：`bin/users/<user>/SelfStockCache.json` 与
   `transaction/xiadan-plus/selfstock_*.json`。本版只操作前者。
4. `TSP → 同花顺自选` 只做加法：在 TSP 删一只不会让同花顺少一只，删除请直接在同花顺里删。
5. 写自选后必须**重启同花顺**才被读入；写板块必须在**同花顺关闭期间**写入才会在下次启动被读入。
6. 屏幕识别依赖三件事：下单端窗口标题含「网上股票交易系统」或「下单」（如「专业版下单」）、
   窗口未被完全遮挡、本机有 Tesseract（默认找 `C:\Program Files\Tesseract-OCR\tesseract.exe`）。
   换了窗口尺寸或改版换布局，列心需要重新聚类（代码里是动态聚类，不是写死像素）。
7. 屏幕识别结果缓存 4 秒，前端 10 秒轮询，所以持仓变化最多滞后约 4 秒。

### 合规

- 上游 tick-stock-panel，作者 shy3130，MIT。`LICENSE` 完整保留，未修改。
- 派生版本标记见 `NOTICE.md`。
- 本分支不含任何账号、密码、Token、持仓数据；同花顺路径通过环境变量 `THS_ROOT` 可覆盖，默认值为本机安装路径。
