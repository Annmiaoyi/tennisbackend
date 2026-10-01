@echo off
REM ---------------------------------------------------------------------------
REM  AceMate Backend Web - launcher
REM  NOTE: this file is intentionally pure ASCII. Windows cmd.exe reads .cmd
REM        files using the OEM code page (GBK on zh-CN), so non-ASCII text here
REM        would be garbled and even executed as commands. All localized
REM        messages live in run.py (Python prints them correctly).
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo.
    echo  [ERROR] Virtual environment not found: .venv
    echo.
    echo   Run setup.cmd first to prepare the environment.
    echo.
    pause
    exit /b 1
)

"%PY%" "%~dp0run.py" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo  [ERROR] Exit code: %RC%
    echo.
)
pause
exit /b %RC%
