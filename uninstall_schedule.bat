@echo off
chcp 65001 >nul
:: 取消每日自动更新计划任务（以管理员身份运行）
schtasks /delete /tn "StockAnalysisDaily" /f
if %errorlevel%==0 (
  echo 已移除“StockAnalysisDaily”自动更新计划任务。
) else (
  echo 未找到该任务，或删除失败（可能需以管理员身份运行）。
)
pause
