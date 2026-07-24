@echo off
title CryptoHybrid A.I. - Port 5041
cd /d C:\Users\abc\Desktop\CryptoHybridAI
start /min "CryptoHybrid A.I. Dashboard" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_btc.py
start /min "CryptoHybrid A.I. Engine" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe watchdog_btc.py
