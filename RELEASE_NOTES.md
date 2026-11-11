## v0.3.34-koolook.3（基于 upstream v0.3.34）

> 说明：v0.3.34-koolook.1 / .2 的产物是未替换的官方 demo 空壳，
> 未包含同步功能；本版 .3 起为实测可跑的完整实现。

### 新增：同花顺远航版自选股 / 持仓同步

本分支不改动 TSP 服务端逻辑、不重新打包桌面客户端，以外围零侵入方式接入：

- **同步桥** `tools/ths_tsp_sync.py`：纯后台 Python 进程，只读本机同花顺缓存文件与
  本机 v1 服务的 `/api/positions`，通过 TSP 原生 `POST /api/watchlist/batch` 写入自选池。
  不做任何 UI 模拟、不抢焦点、不影响日常使用电脑。
- **页面插件** `frontend/page-pack-template/src/extension.tsx`：侧栏新增「同花顺同步」页，
  展示两池状态、持仓明细（含费成本）、上次同步结果，并提供「一键同步」按钮。
  产物 `dist/ths-sync-pack-extension.js`。

### 两池口径（对齐原盯盘系统铁律）

| 分组 | 颜色 | 来源 | 用途 |
| --- | --- | --- | --- |
| 同花顺自选 | sky | `SelfStockCache.json` + 下单端 `selfstock_*.json` 并集 | 盯买点 |
| 同花顺持仓 | rose | v1 `/api/positions`（含费成本） | 盯卖点 |

桥只推增量、只移除自己写入且已不在两池的标的；用户手动加入的标的与自建分组不受影响。
同花顺缓存或 v1 服务暂时不可读时沿用上次快照，绝不误清空自选池。

### 使用（本机 Windows）

1. **自动同步**：双击 `tools/run_sync_loop.bat`，等于
   `pythonw ths_tsp_sync.py --loop`——隐藏窗口后台运行，默认每 60 秒一轮，
   启动即先跑一轮。可选 `python ths_tsp_sync.py --install` 写入开机自启。
2. **一键同步**：双击 `tools/run_sync_once.bat`，跑一轮即退出。
3. **页面插件**：TSP「设置 → 导入与备份 / 页面插件」上传
   `dist/ths-sync-pack-extension.js` → 启用 → 刷新页面，侧栏出现「同花顺同步」。
   页面同时提供「一键同步」按钮（调用桥的 `127.0.0.1:17652/api/sync`）。
4. 配置见 `tools/ths_tsp_sync.json.example`；桌面端无鉴权，`token` 留空即可。
   Docker / 开启鉴权的部署可填 Bearer Token。

### 验证记录（2026-11-11）

- `--dry-run`：读到自选 79 只 / 持仓 7 只 / 合并 79 只，51ms，不写入
- `--once`：`ok=true`，新增 79 只，自动建两个分组，180ms，无错误
- `--loop` 第二轮：`added=[]`——增量幂等，未重复写入、未重排列表
- `POST /api/watchlist/batch` 读回 79 只，`group_ids` 双组归属正确
- 插件上传 `enabled=true`，`GET /api/ext-pages/{id}/bundle.js` 200 且含页面文案

### 合规

- MIT 许可证，上游 `LICENSE` 完整保留；派生说明见 `NOTICE.md`
- 仓库不含 token、账号、SMTP 授权码、运行时数据库与自选股缓存
