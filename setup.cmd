@echo off
REM ---------------------------------------------------------------------------
REM  AceMate Backend Web - one-time environment setup
REM  NOTE: pure ASCII by design - cmd.exe reads .cmd using the OEM code page
REM        (GBK on zh-CN), so non-ASCII text here would be garbled and could
REM        even be executed as commands. Localized messages live in run.py.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

echo ============================================================
echo  AceMate Backend Web - one-time setup
echo ============================================================
echo.

REM ---------------------- 1/4  Python venv ----------------------
if exist ".venv\Scripts\python.exe" goto venv_ok

echo [1/4] Creating virtual environment .venv ...
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -m venv .venv
goto venv_check

:use_python
python -m venv .venv

:venv_check
if not exist ".venv\Scripts\python.exe" goto err_python
echo       done.
goto venv_done

:venv_ok
echo [1/4] venv .venv already exists - skipping.

:venv_done

REM ---------------------- 2/4  Python deps ----------------------
echo [2/4] Installing Python dependencies ...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto err_pip
echo       done.

REM ---------------------- 3/4  frontend toolchain ---------------
echo [3/4] Installing frontend toolchain (tailwindcss) ...
where npm >nul 2>nul
if errorlevel 1 goto err_npm
call npm ci
if errorlevel 1 goto err_npmci
echo       done.

REM ---------------------- 4/4  build CSS ------------------------
echo [4/4] Building Tailwind CSS ...
call npm run build:css
if errorlevel 1 goto err_css
echo       done.

echo.
echo ============================================================
echo  Setup complete.
echo ============================================================
echo.
echo   Next steps:
echo     start.cmd --seed     first run  (create DB + demo data)
echo     start.cmd            normal run (keep existing data)
echo.
pause
exit /b 0

REM ============================ errors ============================
:err_python
echo.
echo  [ERROR] Could not create .venv - Python 3.9+ is required.
echo          Download: https://www.python.org/downloads/
echo.
pause
exit /b 1

:err_pip
echo.
echo  [ERROR] pip install failed - check your network or proxy.
echo.
pause
exit /b 1

:err_npm
echo.
echo  [ERROR] npm not found - Node.js 18+ is required.
echo          Download: https://nodejs.org/
echo.
pause
exit /b 1

:err_npmci
echo.
echo  [ERROR] npm ci failed.
echo.
pause
exit /b 1

:err_css
echo.
echo  [ERROR] Tailwind CSS build failed.
echo.
pause
exit /b 1
