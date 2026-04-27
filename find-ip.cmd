@echo off
set SCRIPT_DIR=%~dp0
python "%SCRIPT_DIR%selectel_floating_ip.py" create %*
exit /b %errorlevel%
