@echo off
setlocal

set "SCRIPT=%~dp0pmid_docx_to_zotero.py"
set "PICKER=%~dp0pick_zotero_docx.ps1"

if not exist "%SCRIPT%" (
    echo ERROR: Could not find:
    echo "%SCRIPT%"
    pause
    exit /b 1
)

if not exist "%PICKER%" (
    echo ERROR: Could not find:
    echo "%PICKER%"
    pause
    exit /b 1
)

for /f "delims=" %%I in ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PICKER%" "%~dp0"') do set "INPUT=%%I"

if not defined INPUT (
    echo No file selected.
    pause
    exit /b 0
)

echo.
echo Selected:
echo "%INPUT%"
echo.
echo Zotero must be OPEN.
echo.

where py >nul 2>&1
if not errorlevel 1 (
    py -3 "%SCRIPT%" "%INPUT%"
) else (
    python "%SCRIPT%" "%INPUT%"
)

set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo Finished.
) else (
    echo ERROR: Script exited with code %RC%
    echo See the message above for the cause.
)
echo.
pause
exit /b %RC%
