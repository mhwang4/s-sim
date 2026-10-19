@echo off
rem KRIHS S-Sim MCP server launcher (Claude Desktop runs this file)
rem Auto-detects QGIS. To force a specific QGIS, set env var QGIS_PYTHON_BAT
rem to the full path of python-qgis-ltr.bat (or python-qgis.bat).
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
    echo [krihs-s-sim] QGIS not found. Install QGIS, or set 1>&2
    echo QGIS_PYTHON_BAT to the full path of python-qgis-ltr.bat. 1>&2
    exit /b 1
)

call "%QGIS_BAT%" "%~dp0server.py" %*
