@echo off
rem Starts the local feed (if not already running) and opens the visualizer in its own window.
setlocal
set "URL=http://127.0.0.1:47800"
where pythonw >nul 2>nul
if errorlevel 1 (start "" /B pyw "%~dp0server.py") else (start "" /B pythonw "%~dp0server.py")
timeout /t 1 /nobreak >nul
set "B=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if exist "%B%" goto open
set "B=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if exist "%B%" goto open
set "B=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if exist "%B%" goto open
start "" %URL%
goto :eof
:open
start "" "%B%" --app=%URL% --window-size=620,400
