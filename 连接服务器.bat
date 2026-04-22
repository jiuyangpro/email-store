@echo off
powershell -ExecutionPolicy Bypass -File "%~dp0connect_server.ps1" %*
pause
