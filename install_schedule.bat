@echo off
chcp 65001 >nul
setlocal
:: 注册 Windows 计划任务：每个交易日 16:10 自动更新股票分析数据
:: 注意：请“以管理员身份运行”一次本脚本以创建计划任务。
:: 周末自动跳过；法定节假日若非交易日，任务仍会运行但仅会重建同一日期，无副作用。

set "HERE=%~dp0"
set "BAT=%HERE%update.bat"
set "TASK=StockAnalysisDaily"

schtasks /query /tn "%TASK%" >nul 2>&1
if %errorlevel%==0 (
  echo 计划任务已存在，先删除旧任务...
  schtasks /delete /tn "%TASK%" /f >nul 2>&1
)

echo 正在注册每日 16:10 自动更新任务（周一至周五）...
schtasks /create /tn "%TASK%" /tr "\"%BAT%\" /silent" /sc weekly /d MON,TUE,WED,THU,FRI /st 16:10 /rl LIMITED
if %errorlevel%==0 (
  echo.
  echo 已成功注册。以后每个交易日盘后自动更新，无需手动操作。
  echo 查看/修改：运行 taskschd.msc ；取消：运行 uninstall_schedule.bat
) else (
  echo.
  echo 注册失败。请右键本文件 ->“以管理员身份运行”后重试。
)
pause
