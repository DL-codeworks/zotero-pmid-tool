@echo off
setlocal
cd /d "%~dp0"

echo Building ZoteroPMIDTool.exe...
echo.

where py >nul 2>&1
if %errorlevel%==0 (
    set "PY=py"
) else (
    set "PY=python"
)

%PY% -m pip show pyinstaller >nul 2>&1
if not %errorlevel%==0 (
    echo PyInstaller is not installed. Installing it now...
    %PY% -m pip install pyinstaller
    if not %errorlevel%==0 (
        echo.
        echo ERROR: Could not install PyInstaller.
        pause
        exit /b 1
    )
)

%PY% -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name ZoteroPMIDTool ^
    gui.py

if not %errorlevel%==0 (
    echo.
    echo ERROR: Build failed.
    pause
    exit /b 1
)

echo.
echo Build complete:
echo "%~dp0dist\ZoteroPMIDTool.exe"
echo.
pause
