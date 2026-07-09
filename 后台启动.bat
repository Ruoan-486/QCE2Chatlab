@echo off
title QCE2ChatLab
cd /d "%~dp0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :15520') do taskkill /f /pid %%a >nul 2>&1
echo Set W=CreateObject("WScript.Shell"):W.CurrentDirectory="%~dp0":W.Run "python main.py --no-browser --port 15520",0,False > "%temp%\qce.vbs"
cscript //nologo "%temp%\qce.vbs"
del "%temp%\qce.vbs"
exit
