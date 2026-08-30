@echo off
chcp 65001 >nul
setlocal
:: 卸载模拟盘执行器计划任务（用户要求：只有主动运行本脚本才算「停止模拟盘」）
schtasks /delete /tn "StockExecutorTrade"  /f >nul 2>&1 && echo   [OK] 已删除 09:25 开仓任务
schtasks /delete /tn "StockExecutorTail"   /f >nul 2>&1 && echo   [OK] 已删除 14:43 尾盘任务
schtasks /delete /tn "StockExecutorReview" /f >nul 2>&1 && echo   [OK] 已删除 15:32 复盘任务
echo 模拟盘已停止。重新启动：双击 install_executor_schedule.bat
pause
