@echo off
powershell -ExecutionPolicy Bypass -Command "Import-Module '%~dp0auto_ssh.ps1'; Invoke-SSHCommand -Command '%*'"
pause
