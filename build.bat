@echo off
setlocal

echo [1/5] Cleaning up previous builds...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

:: Create a clean Python environment
python -m venv venv
call venv\Scripts\activate.bat

echo [2/5] Installing required packages...
pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller

echo [3/5] Building ADB Explorer...
:: "ADB Explorer.spec" is the single description of the build, so the local
:: build and the release workflow cannot drift apart.
pyinstaller --noconfirm --clean --log-level=ERROR "ADB Explorer.spec"

if %ERRORLEVEL% NEQ 0 (
    echo Build failed with error %ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)

echo [4/5] Cleaning up...
rmdir /s /q "__pycache__" 2>nul
rmdir /s /q "venv" 2>nul

if exist "dist\ADB Explorer.exe" (
    echo [5/5] Build successful! Launching application...
    start "" /B "dist\ADB Explorer.exe"
) else (
    echo [ERROR] Executable not found in dist folder!
    pause
    exit /b 1
)

echo.
echo Done! The application should now be running.
pause
