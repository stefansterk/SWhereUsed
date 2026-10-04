@echo off
rem See what SWhereUsed reads from SOLIDWORKS files (references, configurations, properties, version).
rem Two ways:  - double-click this file, then drag a file INTO the window and press Enter (one after another)
rem            - or drag one or more files ONTO this file in Explorer
rem Uses the Python of SWhereUsed (64-bit). Written without ( ) blocks: paths with ( ) & and spaces are fine.

rem Run in a window that stays open afterwards, whatever happens
if defined WU_TEST_INNER goto run
set "WU_TEST_INNER=1"
cmd /k call "%~f0" %*
exit /b

:run
setlocal
set "PY=%USERPROFILE%\.SWhereUsed\venv\Scripts\python.exe"
set "OUT=%USERPROFILE%\.SWhereUsed\test_output.txt"
set "APP=%~dp0SWhereUsed.py"
if exist "%PY%" goto have_python
echo Start SWhereUsed once first (Start SWhereUsed.bat): that installs the Python it needs.
goto end

:have_python
type nul > "%OUT%"
if "%~1"=="" goto ask

rem ---- files dragged onto the icon
:next
if "%~1"=="" goto shown
echo Reading "%~1" ...
>>"%OUT%" echo ==================== "%~1"
"%PY%" "%APP%" --test "%~1" >>"%OUT%" 2>&1
>>"%OUT%" echo.
shift
goto next

:shown
type "%OUT%"
echo.
echo All of the above is saved in "%OUT%"
goto end

rem ---- double-clicked: ask for files, one after another
:ask
echo.
set "F="
set /p "F=Drag a SOLIDWORKS file into this window and press Enter (just Enter to stop): "
if not defined F goto asked
set "F=%F:"=%"
echo.
echo Reading "%F%" ...
>>"%OUT%" echo ==================== "%F%"
"%PY%" "%APP%" --test "%F%" > "%OUT%.one" 2>&1
type "%OUT%.one"
type "%OUT%.one" >> "%OUT%"
>>"%OUT%" echo.
del "%OUT%.one" >nul 2>nul
goto ask

:asked
echo.
echo Everything shown is also saved in "%OUT%"

:end
echo.
echo You can close this window.
endlocal
