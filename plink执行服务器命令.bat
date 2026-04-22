@echo off
powershell -ExecutionPolicy Bypass -File "%~dp0plink_ssh.ps1" %*
pause
