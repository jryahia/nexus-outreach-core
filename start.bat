@echo off
setlocal EnableExtensions
cd /d "%~dp0"
:: The banner uses box glyphs, so the console has to be on UTF-8 before
:: anything is echoed. Without this cmd renders them as mojibake under the
:: default 437/850 codepage. Verified both ways.
chcp 65001 >nul 2>&1
title NEXUS - Outreach Core
mode con: cols=100 lines=38 >nul 2>&1
cls

:: ---------------------------------------------------------------------
:: Colour setup. The for/f trick captures a real ESC byte so ANSI works in
:: conhost and Windows Terminal alike. If it fails, every colour variable
:: stays empty and the banner degrades to plain text instead of garbage.
:: ---------------------------------------------------------------------
set "CY=" & set "DIM=" & set "OK=" & set "ERR=" & set "WARN=" & set "BOLD=" & set "R="
set "AMB=" & set "WHT="
for /f %%a in ('echo prompt $E ^| cmd') do set "ESC=%%a"
if defined ESC (
    set "CY=%ESC%[96m"
    set "DIM=%ESC%[90m"
    set "OK=%ESC%[92m"
    set "ERR=%ESC%[91m"
    set "WARN=%ESC%[93m"
    set "AMB=%ESC%[38;5;214m"
    set "WHT=%ESC%[97m"
    set "BOLD=%ESC%[1m"
    set "R=%ESC%[0m"
)

:: ---------------------------------------------------------------------
:: Banner. Deliberately free of ^ | < > and ampersands - cmd parses those
:: inside an echo and a single pipe in the art kills the script with an
:: unhelpful syntax error.
:: ---------------------------------------------------------------------
echo.
echo %CY%%BOLD%    ███    █  ███████  █    █  █    █  ███████ %R%
echo %CY%%BOLD%    ████   █  █        █    █  █    █  █       %R%
echo %CY%%BOLD%    █ ██   █  █         █  █   █    █  █       %R%
echo %CY%%BOLD%    █  ██  █  ██████     ██    █    █  ███████ %R%
echo %CY%%BOLD%    █   ██ █  █         ██ █   █    █        █ %R%
echo %CY%%BOLD%    █    ███  █        █   █   █    █        █ %R%
echo %CY%%BOLD%    █     ██  ███████  █    █   ████   ███████ %R%
echo.
echo %WHT%          O U T R E A C H   C O R E%R%
echo %DIM%    ---------------------------------------------------------%R%
echo %AMB%    [ Core Architect: Yahya.Jr ]%R%
echo %DIM%    ---------------------------------------------------------%R%
echo.

:: ---------------------------------------------------------------------
:: Preflight. Check the interpreter itself, not just the .venv folder - a
:: half-created venv passes a directory test and then fails obscurely.
:: ---------------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo %ERR%    [ FAIL ] Virtual environment not found.%R%
    echo.
    echo %DIM%    Expected: %CD%\.venv\Scripts\python.exe%R%
    echo.
    echo     Install the dependencies first, from this folder:
    echo.
    echo %WARN%        python -m venv .venv%R%
    echo %WARN%        .venv\Scripts\python.exe -m pip install -r requirements.txt%R%
    echo.
    echo     Then run this file again.
    echo.
    pause
    exit /b 1
)

if not exist "app.py" (
    echo %ERR%    [ FAIL ] app.py not found next to this script.%R%
    echo %DIM%    Run start.bat from the project root.%R%
    echo.
    pause
    exit /b 1
)

echo %OK%    [  OK  ]%R% Core runtime located
echo %OK%    [  OK  ]%R% app.py located
echo %CY%    [ BOOT ]%R% Spinning up the outreach core
echo %DIM%    [ .... ]%R% Console opens on http://localhost:8501
echo %DIM%    [ .... ]%R% Press Ctrl+C in this window to shut the core down
echo.

.venv\Scripts\python.exe -m streamlit run app.py
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo %ERR%    [ FAIL ] Core exited with code %RC%.%R%
    echo %DIM%    Scroll up for the traceback, then run tools\selftest.py to narrow it down.%R%
) else (
    echo %DIM%    Core offline.%R%
)
echo.
pause
exit /b %RC%
