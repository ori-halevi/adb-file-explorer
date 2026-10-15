@echo off
:: Start ADB Explorer straight from the source - no build, no packaging.
:: The first run sets up a local environment if PyQt6 is not already there;
:: after that it starts immediately.
setlocal
cd /d "%~dp0"

set "VENV_PYW=venv\Scripts\pythonw.exe"
set "VENV_PY=venv\Scripts\python.exe"

:: 1. a local environment from an earlier run, or from build.bat
if exist "%VENV_PYW%" (
    start "" "%VENV_PYW%" "main.py"
    exit /b 0
)

:: 2. a system Python that already has PyQt6
for %%P in (pythonw.exe) do set "SYS_PYW=%%~$PATH:P"
if defined SYS_PYW (
    python -c "import PyQt6" >nul 2>nul
    if not errorlevel 1 (
        start "" "%SYS_PYW%" "main.py"
        exit /b 0
    )
)

:: 3. nothing to run with yet, so make one
echo First run: creating a local environment for ADB Explorer...
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv venv
) else (
    python -m venv venv
)

if not exist "%VENV_PY%" (
    echo.
    echo Could not create the environment. Install Python 3.10 or newer from
    echo https://www.python.org/downloads/ and run this again.
    pause
    exit /b 1
)

echo Installing PyQt6...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>nul
"%VENV_PY%" -m pip install PyQt6
if errorlevel 1 (
    echo.
    echo PyQt6 could not be installed. Check the connection and try again.
    pause
    exit /b 1
)

echo Done. Starting ADB Explorer...
start "" "%VENV_PYW%" "main.py"
exit /b 0
