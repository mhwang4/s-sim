@echo off
rem KRIHS S-Sim MCP server installer
rem  1] find QGIS  2] pip install mcp into QGIS python  3] register in Claude Desktop
setlocal enabledelayedexpansion

set "QGIS_BAT=%QGIS_PYTHON_BAT%"
if not defined QGIS_BAT (
    for /d %%D in ("C:\Program Files\QGIS*" "C:\OSGeo4W*") do (
        if exist "%%D\bin\python-qgis-ltr.bat" (
            set "QGIS_BAT=%%D\bin\python-qgis-ltr.bat"
        ) else if exist "%%D\bin\python-qgis.bat" (
            set "QGIS_BAT=%%D\bin\python-qgis.bat"
        )
    )
)
if not defined QGIS_BAT (
    echo [1/3] FAIL: QGIS not found. Install QGIS first: https://qgis.org
    pause
    exit /b 1
)
echo [1/3] QGIS found: %QGIS_BAT%

echo [2/3] Installing mcp package into QGIS python...
call "%QGIS_BAT%" -m pip install mcp --disable-pip-version-check --quiet --no-warn-script-location
if errorlevel 1 (
    echo FAIL: pip install mcp error. Check internet connection or proxy.
    pause
    exit /b 1
)

echo [3/3] Registering krihs-s-sim in Claude Desktop config...
rem QGIS python launcher resets PATH, so call PowerShell by full path
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_claude.ps1" "%~dp0run_server.bat"
if errorlevel 1 (
    echo FAIL: Claude Desktop registration error.
    pause
    exit /b 1
)

echo.
echo Done. Quit Claude Desktop completely (tray icon too) and start it again.
pause
