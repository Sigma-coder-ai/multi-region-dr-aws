@echo off
rem Double-click to start the local DR demo (SIMULATED, offline, $0) and open the dashboard.
title DR System - Local Demo
cd /d "%~dp0"

call :setup || goto :fail

echo.
echo  Starting the simulated two-region system (Mumbai + Singapore)...
echo  A second window will open: keep it open while you use the dashboard.
echo  To stop the demo, close that window (or press Ctrl+C in it).
echo.
start "DR Simulation (close to stop)" cmd /k ".venv\Scripts\python -m drsystem sim"
timeout /t 5 /nobreak >nul
start "" "http://127.0.0.1:8100/"
echo  Dashboard opened in your browser: http://127.0.0.1:8100/
echo.
timeout /t 5 >nul
exit /b 0

:setup
if exist ".venv\Scripts\python.exe" exit /b 0
echo  First run: setting up Python environment (takes 1-2 minutes, only once)...
where python >nul 2>nul || (echo  Python 3.12+ is not installed or not on PATH. & exit /b 1)
python -m venv .venv || exit /b 1
".venv\Scripts\python" -m pip install --quiet --upgrade pip
".venv\Scripts\python" -m pip install --quiet -e ".[dev]" || exit /b 1
echo  Setup complete.
exit /b 0

:fail
echo.
echo  Setup failed. Make sure Python 3.12 is installed, then try again.
pause
exit /b 1
