@echo off
REM ===================================================================
REM  Job Market Research Platform - Windows launcher
REM  First run creates a virtual environment and installs dependencies.
REM  Every run after that starts in a couple of seconds.
REM ===================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python was not found on your PATH.
  echo   Install Python 3.11 or newer from https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH" during installation.
  echo.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo.
  echo   First-run setup. This takes a few minutes, once only.
  echo.
  python -m venv .venv
  if errorlevel 1 goto :venvfail
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  pip install -r requirements.txt
  if errorlevel 1 goto :depsfail
  echo.
  echo   Setup complete.
  echo.
) else (
  call .venv\Scripts\activate.bat
)

echo   Starting the application...
python -m backend.app.main
goto :eof

:venvfail
echo.
echo   Could not create the virtual environment.
echo   Try running: python -m pip install --user virtualenv
echo.
pause
exit /b 1

:depsfail
echo.
echo   Dependency installation failed.
echo   Check your internet connection and run this file again.
echo.
pause
exit /b 1
