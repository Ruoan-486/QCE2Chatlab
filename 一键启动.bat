@echo off
cd /d "%~dp0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :15520') do taskkill /f /pid %%a >nul 2>&1

:: create VBS to start everything, open browser, then vanish
>"%temp%\qce_all.vbs" echo Set W=CreateObject("WScript.Shell"):W.CurrentDirectory="%~dp0":W.Run """C:\Users\4uoan\Desktop\ChatLab.lnk""",0,False:W.Run """C:\Users\4uoan\Desktop\QCE v5 - QQ Chat Export Tool.lnk""",0,False:W.Run """C:\Users\4uoan\Desktop\QCE.lnk""",0,False:W.Run "python main.py --no-browser --port 15520",0,False:WScript.Sleep 4000:W.Run "cmd /c start http://127.0.0.1:15520",0,False
start "" wscript "%temp%\qce_all.vbs"
exit
