@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=.\.venv\Scripts\python.exe"
set "MAIN_SCRIPT=main.py"

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Missing Python: "%PYTHON_EXE%"
  exit /b 1
)

if not exist "%MAIN_SCRIPT%" (
  echo [ERROR] Missing script: "%MAIN_SCRIPT%"
  exit /b 1
)

echo [INFO] Starting app in foreground...
echo [INFO] Logs: console + logs\app.log
"%PYTHON_EXE%" -u "%MAIN_SCRIPT%" %*
exit /b %ERRORLEVEL%
