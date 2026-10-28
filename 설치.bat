@echo off
setlocal

set "PLUGIN_NAME=KRIHS_S_Sim"
set "SRC=%~dp0%PLUGIN_NAME%"
set "DST=%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\%PLUGIN_NAME%"

echo ==================================================
echo  KRIHS S-Sim 플러그인 설치
echo ==================================================
echo.

rem 경로에 괄호가 있으면(예: D:\(일반) ...) 괄호 블록 안의 %SRC%가 블록을 닫아 버리므로 블록을 쓰지 않음
if exist "%SRC%\metadata.txt" goto copy
echo [오류] 설치할 플러그인 폴더를 찾을 수 없습니다.
echo        압축을 푼 폴더 안에서 이 파일을 실행하세요.
echo        확인 경로: %SRC%
pause
exit /b 1

:copy

echo 설치 위치: %DST%
echo QGIS가 켜져 있으면 먼저 종료하세요.
echo.

robocopy "%SRC%" "%DST%" /E /PURGE /XD __pycache__ /NFL /NDL /NJH /NJS >nul
if %ERRORLEVEL% GEQ 8 (
    echo [오류] 파일 복사에 실패했습니다. robocopy 코드: %ERRORLEVEL%
    echo        QGIS를 종료한 뒤 다시 실행하세요.
    pause
    exit /b 1
)

echo [완료] 플러그인을 설치했습니다.
echo.
echo 다음 순서로 진행하세요.
echo   1. QGIS를 실행(또는 재시작)합니다.
echo   2. 메뉴 [플러그인 - 플러그인 관리 및 설치]에서 "KRIHS S-Sim"을 체크해 활성화합니다.
echo   3. 툴바의 S-Sim 아이콘으로 창을 엽니다.
echo   4. [분석 지역 설정]에서 데이터팩 폴더로 이 묶음의 S-Sim_데이터팩 을 지정하고
echo      지역과 결과 저장 폴더를 고른 뒤 [지역 적용]을 누릅니다.
echo   5. Claude에서 대화로 쓰려면 mcp_server\설치_mcp.bat 을 실행합니다(선택).
echo.
pause
