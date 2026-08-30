@echo off
chcp 65001 >nul
setlocal
:: ============================================================
:: 模拟盘执行器计划任务一键安装（2026-08-30）
:: 注册 3 个独立 Windows 计划任务（比常驻窗口更稳，不怕误关窗口）：
::   StockExecutorTrade  交易日 09:25  开仓通道（竞价决策线裁决）
::   StockExecutorTail   交易日 14:43  尾盘确认通道（持仓管理+微红入场）
::   StockExecutorReview 交易日 15:32  当日复盘总结（盈亏归因+明日方案）
:: 电脑需在对应时间处于开机状态；未开机则当日跳过（次日任务照常）。
:: 卸载： uninstall_executor_schedule.bat
:: ============================================================

set "HERE=%~dp0"
set "PY=C:\Users\Basshunter-j\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"
set "RUNNER=%HERE%runner.py"

echo 正在注册模拟盘执行器计划任务...
schtasks /create /tn "StockExecutorTrade"  /tr "\"%PY%\" \"%RUNNER%\" --now"    /sc weekly /d MON,TUE,WED,THU,FRI /st 09:25 /rl LIMITED /f
if %errorlevel%==0 (echo   [OK] 09:25 开仓通道) else (echo   [FAIL] 09:25 开仓通道注册失败)
schtasks /create /tn "StockExecutorTail"   /tr "\"%PY%\" \"%RUNNER%\" --tail"   /sc weekly /d MON,TUE,WED,THU,FRI /st 14:43 /rl LIMITED /f
if %errorlevel%==0 (echo   [OK] 14:43 尾盘确认) else (echo   [FAIL] 14:43 尾盘确认注册失败)
schtasks /create /tn "StockExecutorReview" /tr "\"%PY%\" \"%RUNNER%\" --review" /sc weekly /d MON,TUE,WED,THU,FRI /st 15:32 /rl LIMITED /f
if %errorlevel%==0 (echo   [OK] 15:32 复盘总结) else (echo   [FAIL] 15:32 复盘总结注册失败)

echo.
echo 完成。模拟盘将按以下节奏自动运行（每周一至周五）：
echo   09:25 开仓（竞价决策线） / 14:43 尾盘确认 / 15:32 复盘推送
echo 注意：需电脑处于开机状态；想停止模拟盘请运行 uninstall_executor_schedule.bat
pause
