@echo off
title CryptoHybrid AI -- Service Mode
cd /d %~dp0

echo Starting CryptoHybrid AI in service mode (Task Scheduler)...

echo Cleaning up any existing CryptoHybrid processes...
powershell -NoProfile -Command "Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like '*dashboard_btc.py*' -or $_.CommandLine -like '*watchdog_btc.py*' -or $_.CommandLine -like '*main_cryptohybrid.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" > nul 2>&1
ping -n 3 127.0.0.1 > nul

start /B python dashboard_btc.py

ping -n 11 127.0.0.1 > nul

start /B python watchdog_btc.py

echo CryptoHybrid AI launched in background -- dashboard + watchdog running.
exit /b 0
