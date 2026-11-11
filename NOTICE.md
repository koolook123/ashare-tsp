# NOTICE

本项目是 [tick-stock-panel](https://github.com/shy3130/tick-stock-panel) 的二次开发分支（fork）。

- 上游项目：tick-stock-panel，作者/维护者 shy3130，许可证 MIT。
- 上游版权声明与许可全文见仓库根目录 `LICENSE`，本分支完整保留，未作修改。
- 本分支新增内容：`tools/ths_tsp_sync.py`、`tools/ths_tsp_sync.json.example`、
  `tools/run_sync_once.bat`、`tools/run_sync_loop.bat`、
  `frontend/page-pack-template/src/extension.tsx`（同花顺同步页面插件）、
  `RELEASE_NOTES.md`、`NOTICE.md`，以及配套文档。
  新增部分随本项目同样以 MIT 许可提供。

本分支仅用于个人本地盯盘：同步桥只读本机同花顺缓存文件与本机 v1 服务的
`/api/positions`，写入本机 TSP 自选池；不修改上游服务端逻辑，不重新分发
上游桌面客户端二进制，不含任何第三方凭据、账号或运行时数据。

派生版本标记：`v0.3.34-koolook.3`（基于 upstream `v0.3.34`）。
`v0.3.34-koolook.1` / `.2` 的产物是未替换的官方 demo 空壳，不含同步功能，已由 `.3` 取代。
