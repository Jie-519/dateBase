@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 goto nopy
python "%~dp0setup_env.py" %*
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
pause
exit /b %RC%
