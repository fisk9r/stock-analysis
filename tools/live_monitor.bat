@echo off
rem 盘中实时盯盘（5秒轮询，交易时段自动生效，非交易时段待机）
cd /d "%~dp0.."
python tools\live_monitor.py
pause
