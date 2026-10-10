@echo off
cd /d "%~dp0"
setlocal

REM ===================== Settings =====================
set HOST=127.0.0.1
set PORT=8080
set SECURE=1
set EXTRA=
set OPEN_BROWSER=1

REM Secure mode is ON by default (SECURE=1):
REM   every endpoint needs an accesstoken header except /v2/register.
REM   Get a token first:  GET /v2/register?name=Kashier1
REM   then send header:   accesstoken: <token>
REM   In /ui, open the "Security token" card and click Register once.
REM Turn security off    : set SECURE=0
REM Real device testing  : set HOST=0.0.0.0
REM Plain HTTP no TLS    : set EXTRA=--http
REM Fast state machine   : set EXTRA=--fast
REM Do not open browser  : set OPEN_BROWSER=0
REM Full docs (Chinese)  : see README.md
REM ===================================================

REM ---------- locate python ----------
set PY=
where python >nul 2>nul
if %errorlevel%==0 set PY=python
if "%PY%"=="" (
    where py >nul 2>nul
    if %errorlevel%==0 set PY=py -3
)
if "%PY%"=="" (
    echo [ERROR] Python not found. Install Python and add it to PATH.
    echo.
    pause
    exit /b 1
)

REM ---------- warn if the port is already in use ----------
netstat -ano | findstr ":%PORT% " | findstr LISTENING >nul
if %errorlevel%==0 (
    echo [WARN] Port %PORT% is already listening - another instance may be running.
    echo        If startup fails, pick another port or stop the old process:
    echo          netstat -ano ^| findstr :%PORT%
    echo          taskkill /PID ^<PID^> /F
    echo.
)

REM ---------- browser host (0.0.0.0 cannot be opened) ----------
set BROWSER_HOST=%HOST%
if "%HOST%"=="0.0.0.0" set BROWSER_HOST=127.0.0.1

REM ---------- build arguments ----------
set ARGS=--host %HOST% --port %PORT%
if "%SECURE%"=="1" (
    set ARGS=%ARGS% --secure --allow-open-reset
    set MODE=HTTPS + SECURE
) else (
    set MODE=HTTPS
)

echo ================================================================
echo   Kaspi POS Simulator   [%MODE%]
echo.
echo   API        https://%BROWSER_HOST%:%PORT%/
echo   Console    https://%BROWSER_HOST%:%PORT%/ui
echo   Listening  %HOST%:%PORT%
echo   Cert       %~dp0.certs\cert.pem   (auto-created on first run)
echo.
echo   First browser visit warns about the self-signed cert:
echo   click  Advanced  -  Continue
echo   Stop the server with Ctrl+C in this window.
echo ================================================================
echo.
if "%SECURE%"=="1" (
    echo   SECURE MODE: all calls except /v2/register need the accesstoken header.
    echo   1. Get a token : https://%BROWSER_HOST%:%PORT%/v2/register?name=Kashier1
    echo   2. Send header : accesstoken: ^<token^>
    echo   Missing or expired token -^> HTTP 401 / 403 with statusCode 999.
    echo   /v2/reset stays open so the /ui Reset button keeps working.
    echo.
)

if "%OPEN_BROWSER%"=="1" start "" https://%BROWSER_HOST%:%PORT%/ui

%PY% kaspi_pos_simulator.py %ARGS% %EXTRA%

echo.
echo Server stopped.
pause
