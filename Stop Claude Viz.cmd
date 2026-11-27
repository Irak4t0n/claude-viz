@echo off
rem Stops the local feed and its helper processes.
for /f "tokens=5" %%a in ('netstat -ano ^| findstr 127.0.0.1:47800 ^| findstr LISTENING') do taskkill /PID %%a /T /F
