@echo off
chcp 65001 >nul
REM 同花顺 → TSP 自动同步（隐藏窗口后台运行，每 60 秒一轮，不抢焦点）
cd /d "%~dp0"

set "PYW=C:\Python314\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"

echo 已启动后台自动同步：每 60 秒把同花顺自选股/持仓同步到 TSP
echo 状态页：TSP 侧栏「同花顺同步」；日志：%~dp0ths_tsp_sync.log
echo 停止：任务管理器结束 pythonw.exe 对应的「ths_tsp_sync.py --loop」进程

start "" "%PYW%" ths_tsp_sync.py --loop %*
