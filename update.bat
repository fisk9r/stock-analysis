@echo off
chcp 65001 >nul
setlocal
:: 一键更新：拉取当日收盘后行情 + 盘后快照 + 板块成分，并重建前端数据 dist\data.js
:: 说明：本脚本需要可访问东方财富公开行情接口的网络环境；
::       生成的 dist\index.html 为纯静态、零依赖，可离线/内网打开。
:: 建议：交易日 16:00 后运行（也可由 install_schedule.bat 自动每日执行）。

set "PY=C:\Users\Basshunter-j\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

cd /d "%~dp0"

echo [1/2] 拉取全市场清单 / 日K / 盘后快照 + 刷新板块成分映射 ...
"%PY%" pipeline\fetch.py --boards
if errorlevel 1 (
  echo 抓取阶段出现错误（可能网络受限），但会尽量用已有缓存继续。
)

echo [2/2] 运行分析引擎，生成 dist\data.js ...
"%PY%" pipeline\build.py
if errorlevel 1 (
  echo 构建失败，请检查 pipeline\build.py 报错信息。
  pause
  exit /b 1
)

echo.
echo 完成。请用浏览器打开 dist\index.html 查看分析结果。
echo （数据基准日见页面顶部“交易日”。如需指定日期：python pipeline\build.py --date=YYYY-MM-DD）
if "%~1"=="/silent" exit /b 0
pause
