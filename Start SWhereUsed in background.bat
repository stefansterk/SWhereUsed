@echo off
rem Starts SWhereUsed without a window: it runs in the background, with an icon by the clock
rem (double-click it to open SWhereUsed, right-click it to stop). Start SWhereUsed.bat shows the window instead.
set "PYW=%USERPROFILE%\.SWhereUsed\venv\Scripts\pythonw.exe"
if defined SWHEREUSED_HOME set "PYW=%SWHEREUSED_HOME%\venv\Scripts\pythonw.exe"
if not exist "%PYW%" goto setup
start "" "%PYW%" "%~dp0SWhereUsed.py" --background
exit /b

:setup
echo Start SWhereUsed once with "Start SWhereUsed.bat" first: that installs what it needs.
pause
