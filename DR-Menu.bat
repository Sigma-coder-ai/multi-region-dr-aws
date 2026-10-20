@echo off
rem Double-click for a menu of every common task (local demo, tests, drills, real AWS).
title DR System - Menu
cd /d "%~dp0"
set PROFILE=mahesh-project
set PY=.venv\Scripts\python

call :setup || (echo Setup failed. Install Python 3.12 and try again. & pause & exit /b 1)

:menu
cls
echo  ================================================================
echo   Intelligent Multi-Region Disaster Recovery System
echo  ================================================================
echo   LOCAL (simulated, offline, $0)
echo     1  Start live demo dashboard
echo     2  Run all tests
echo     3  Run simulated drills (all scenarios, with failback)
echo     4  Show recovery-time optimizer (model)
echo.
echo   REAL AWS (Free plan, $0 out-of-pocket)
echo     5  Sign in to AWS (aws login)
echo     6  Status of both regions
echo     7  Zero-spend check (plan, credits, days left)
echo     8  Real drill: hard outage + failback  (~15 min)
echo     9  Real drill: gray failure + failback (~15 min)
echo     T  TEAR DOWN everything on AWS
echo.
echo     R  Open results folder        D  Open docs folder
echo     0  Exit
echo  ================================================================
set "c="
set /p c=  Choose:
if "%c%"=="1" goto demo
if "%c%"=="2" goto tests
if "%c%"=="3" goto simdrills
if "%c%"=="4" goto optimize
if "%c%"=="5" goto login
if "%c%"=="6" goto status
if "%c%"=="7" goto preflight
if "%c%"=="8" (set SC=hard-outage& goto realdrill)
if "%c%"=="9" (set SC=gray-errors& goto realdrill)
if /i "%c%"=="T" goto teardown
if /i "%c%"=="R" (start "" explorer "%~dp0evidence" & goto menu)
if /i "%c%"=="D" (start "" explorer "%~dp0docs" & goto menu)
if "%c%"=="0" exit /b 0
goto menu

:demo
start "DR Simulation (close to stop)" cmd /k "%PY% -m drsystem sim"
timeout /t 5 /nobreak >nul
start "" "http://127.0.0.1:8100/"
echo  Dashboard opened. Close the "DR Simulation" window to stop it.
goto done

:tests
%PY% -m pytest -q -W ignore::DeprecationWarning
goto done

:simdrills
%PY% -m drsystem drill all --repeat 3 --failback
goto done

:optimize
%PY% -m drsystem optimize --target-rto 300 --target-rpo 60 --replication-lag 26
start "" notepad "%~dp0evidence\model\optimizer.md"
goto done

:login
aws login --profile %PROFILE%
goto done

:status
%PY% scripts\aws_deploy.py status --profile %PROFILE%
if errorlevel 1 echo  (If it says the session expired, choose 5 to sign in first.)
goto done

:preflight
%PY% -m drsystem preflight --profile %PROFILE%
goto done

:realdrill
echo  Checking zero-spend gate first...
%PY% -m drsystem preflight --profile %PROFILE% | findstr /c:"RESULT: OK" >nul || (echo  Preflight failed: drill NOT started. Choose 7 to see why. & goto done)
echo  Waiting for a healthy baseline, then running the %SC% drill. Do not close this window.
%PY% scripts\wait_healthy.py 900 && %PY% scripts\aws_drill.py %SC% --profile %PROFILE% --failback
goto done

:teardown
echo.
echo  This DELETES the whole AWS deployment (buckets, Lambdas, schedule, roles, logs).
set "ok="
set /p ok=  Type DELETE to confirm:
if not "%ok%"=="DELETE" (echo  Cancelled. & goto done)
%PY% scripts\aws_deploy.py teardown --profile %PROFILE%
goto done

:done
echo.
pause
goto menu

:setup
if exist ".venv\Scripts\python.exe" exit /b 0
echo  First run: setting up Python environment (1-2 minutes, only once)...
where python >nul 2>nul || exit /b 1
python -m venv .venv || exit /b 1
".venv\Scripts\python" -m pip install --quiet --upgrade pip
".venv\Scripts\python" -m pip install --quiet -e ".[dev]" || exit /b 1
exit /b 0
