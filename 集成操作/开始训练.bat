@echo off
chcp 65001 >nul
setlocal
title C2DB Integrator - enter counts to start training
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopy
python "%~dp0run.py" %*
set RC=%errorlevel%
goto done

:nopy
echo.
echo [ERROR] Python not found.
echo Install Python 3.11+ and check "Add python.exe to PATH".
echo.
pause
exit /b 1

:done
echo.
if not "%RC%"=="0" (
  echo [FAILED] exit code %RC% - see messages above.
  echo Artifacts remain on disk; fix and rerun.
) else (
  echo [DONE] finished - the exhibit path was printed above.
)
echo.
pause
