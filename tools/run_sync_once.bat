@echo off
chcp 65001 >nul
REM 同花顺 → TSP 一键同步（只读文件，不抢焦点，不影响你使用电脑）
cd /d "%~dp0"

set "PY=C:\Python314\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================
echo  同花顺远航版 -^> TSP 一键同步
echo ============================================
"%PY%" ths_tsp_sync.py --once %*
echo.
echo 完成。日志：%~dp0ths_tsp_sync.log
pause
