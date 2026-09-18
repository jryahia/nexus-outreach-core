@echo off
setlocal EnableExtensions
cd /d "%~dp0"
:: Everything below is relative to this file, so the project can be cloned or
:: unzipped anywhere - Desktop, another drive, a path with spaces - and still
:: run. No absolute paths, no assumptions about the current directory.

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
echo %CY%%BOLD%    ###    #  #######  #    #  #    #  ####### %R%
echo %CY%%BOLD%    ####   #  #        #    #  #    #  #       %R%
echo %CY%%BOLD%    # ##   #  #         #  #   #    #  #       %R%
echo %CY%%BOLD%    #  ##  #  ######     ##    #    #  ####### %R%
echo %CY%%BOLD%    #   ## #  #         ## #   #    #        # %R%
echo %CY%%BOLD%    #    ###  #        #   #   #    #        # %R%
echo %CY%%BOLD%    #     ##  #######  #    #   ####   ####### %R%
echo.
echo %WHT%          O U T R E A C H   C O R E%R%
echo %DIM%    ---------------------------------------------------------%R%
echo %AMB%    [ Core Architect: Yahya.Jr ]%R%
echo %DIM%    ---------------------------------------------------------%R%
echo.

if not exist "app.py" (
    echo %ERR%    [ FAIL ] app.py not found next to this script.%R%
    echo %DIM%    Keep start.bat in the project root, beside app.py.%R%
    echo.
    pause
    exit /b 1
)

:: Already set up? Skip straight to launch.
if exist ".venv\Scripts\python.exe" goto :launch

:: =====================================================================
:: FIRST RUN - build the environment from nothing
:: =====================================================================
echo %WARN%    [ SETUP ] No environment found. Building one now.%R%
echo %DIM%    This happens once and takes a few minutes.%R%
echo.

:: Find an interpreter. "py" (the Windows launcher) is tried first because a
:: bare "python" on a stock machine is often the Microsoft Store stub, which
:: exits without doing anything useful.
set "BOOTPY="
py -3 -c "import sys" >nul 2>&1 && set "BOOTPY=py -3"
if not defined BOOTPY (
    python -c "import sys" >nul 2>&1 && set "BOOTPY=python"
)
if not defined BOOTPY (
    echo %ERR%    [ FAIL ] Python was not found on this machine.%R%
    echo.
    echo     Install Python 3.10 or newer, then run this file again:
    echo %WARN%        https://www.python.org/downloads/%R%
    echo.
    echo %DIM%    During installation, tick "Add python.exe to PATH".%R%
    echo.
    pause
    exit /b 1
)

:: Version gate. The code uses 3.10+ syntax, so an older machine is told
:: plainly here rather than hitting a SyntaxError three screens later.
%BOOTPY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo %ERR%    [ FAIL ] Python 3.10 or newer is required.%R%
    for /f "tokens=*" %%v in ('%BOOTPY% -c "import sys; print(sys.version.split()[0])" 2^>nul') do echo %DIM%    Found version %%v%R%
    echo.
    echo %WARN%        https://www.python.org/downloads/%R%
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('%BOOTPY% -c "import sys; print(sys.version.split()[0])" 2^>nul') do echo %OK%    [  OK  ]%R% Python %%v detected

echo %DIM%    [ .... ]%R% Creating the virtual environment
%BOOTPY% -m venv .venv
if errorlevel 1 goto :venv_failed
if not exist ".venv\Scripts\python.exe" goto :venv_failed
echo %OK%    [  OK  ]%R% Virtual environment created

echo %DIM%    [ .... ]%R% Updating pip
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
:: A stale pip is a warning, never a stopper - carry on regardless.

echo %DIM%    [ .... ]%R% Installing dependencies from requirements.txt
echo %DIM%             Streamlit, Plotly, Scrapling and friends. Please wait.%R%
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo %ERR%    [ FAIL ] Dependency installation failed.%R%
    echo %DIM%    Scroll up for the reason. Usually no internet connection, or%R%
    echo %DIM%    a proxy or firewall blocking pypi.org.%R%
    echo.
    echo     To retry from scratch, delete the .venv folder and run this again.
    echo.
    pause
    exit /b 1
)
echo %OK%    [  OK  ]%R% Dependencies installed

:: Scrapling drives a real Chromium. It is a large download, so it is
:: announced rather than done silently, and a failure is not fatal: the app
:: still runs and its Diagnostic tab reports exactly what is missing.
echo %DIM%    [ .... ]%R% Downloading browser engines for the scrapers
echo %DIM%             Roughly 300 MB, once. Skipped if already present.%R%
".venv\Scripts\scrapling.exe" install
if errorlevel 1 (
    echo %WARN%    [ WARN ] Browser engines did not install.%R%
    echo %DIM%    Scraping stays offline until they do. Retry any time with:%R%
    echo %DIM%        .venv\Scripts\scrapling.exe install%R%
    echo %DIM%    Everything else in the app works without them.%R%
) else (
    echo %OK%    [  OK  ]%R% Browser engines ready
)

echo.
echo %OK%    [ READY ]%R% Setup complete.
echo.

:launch
echo %OK%    [  OK  ]%R% Core runtime located
echo %CY%    [ BOOT ]%R% Spinning up the outreach core
echo %DIM%    [ .... ]%R% Console opens on http://localhost:8501
echo %DIM%    [ .... ]%R% Press Ctrl+C in this window to shut the core down
echo.

".venv\Scripts\python.exe" -m streamlit run app.py
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

:venv_failed
echo.
echo %ERR%    [ FAIL ] Could not create the virtual environment.%R%
echo %DIM%    On some Linux-style Python builds the venv module ships separately.%R%
echo.
echo     Try it manually from this folder to see the real error:
echo %WARN%        python -m venv .venv%R%
echo.
pause
exit /b 1
