@echo off
rem SWhereUsed for SOLIDWORKS - double-click to start.
rem The first start sets up a private Python environment (a minute or two). Later starts are quick.
rem Everything it creates lives in %USERPROFILE%\.SWhereUsed, never in this folder.
setlocal EnableExtensions
cd /d "%~dp0"
title SWhereUsed

echo.
echo  SWhereUsed for SOLIDWORKS
echo  ========================
echo.

rem Started from inside the zip file? Windows then runs only this .bat from a temporary folder.
if not exist "%~dp0SWhereUsed.py" goto notunzipped

rem SWhereUsed used to be called WhereUsed: its data folder (index, settings, key, history, Python) moves along, once
if defined SWHEREUSED_HOME goto homeset
if exist "%USERPROFILE%\.SWhereUsed" goto homeset
if not exist "%USERPROFILE%\.whereused" goto homeset
move "%USERPROFILE%\.whereused" "%USERPROFILE%\.SWhereUsed" >nul 2>nul
if exist "%USERPROFILE%\.SWhereUsed" goto moved
echo.
echo  The data folder of WhereUsed could not be moved to .SWhereUsed: the old WhereUsed is probably still running.
echo  Stop it first (close its window, or right-click its icon by the clock and choose Stop), then start SWhereUsed again.
echo.
pause
exit /b
:moved
echo  WhereUsed is now called SWhereUsed: its data folder moved to %USERPROFILE%\.SWhereUsed
:homeset
if defined SWHEREUSED_HOME (set "HOMEDIR=%SWHEREUSED_HOME%") else (set "HOMEDIR=%USERPROFILE%\.SWhereUsed")
set "VENV=%HOMEDIR%\venv"
set "VPY=%VENV%\Scripts\python.exe"

if not exist "%VPY%" goto setup
echo  Checking the Python environment...
"%VPY%" -c "import pythoncom, comtypes" >nul 2>nul
if not errorlevel 1 goto run
echo  The Python environment needs a repair; setting it up again.
rmdir /s /q "%VENV%" >nul 2>nul

:setup
echo  First start: setting up a private Python environment in
echo    %VENV%
echo  This happens only once and takes a minute or two.
echo.
echo  [1/3] Looking for Python...
call :findpython
if not defined PY goto nopython
%PY% -c "import sys; print('        Found: Python', sys.version.split()[0], 'in', sys.executable)"
echo  [2/3] Creating the environment...
%PY% -m venv "%VENV%"
if errorlevel 1 goto fail
echo  [3/3] Installing pywin32 and comtypes (downloads about 10 MB)...
"%VPY%" -m pip install --disable-pip-version-check --upgrade pip >nul 2>nul
"%VPY%" -m pip install --disable-pip-version-check pywin32 comtypes
if errorlevel 1 goto pipfail
echo.
echo  Setup done.
echo.

:run
echo  Starting SWhereUsed. Your browser opens in a moment.
echo  Keep this window open while you use it; close it to stop SWhereUsed.
echo.
set "SWHEREUSED_LAUNCHER=bat"
"%VPY%" "%~dp0SWhereUsed.py" %*
if errorlevel 4 goto stopped
if errorlevel 3 goto updated

:stopped
echo.
echo  SWhereUsed has stopped.
pause
exit /b

:updated
rem SWhereUsed installed a new version and asks to be started again (exit code 3)
echo.
echo  SWhereUsed was updated. Starting the new version...
set "SWHEREUSED_NO_BROWSER=1"
"%VPY%" -m pip install --disable-pip-version-check -q pywin32 comtypes >nul 2>nul
rem A new version of this file comes as "Start SWhereUsed.bat.new": swapped in ONE line, then the new one runs
if exist "%~dp0Start SWhereUsed.bat.new" move /y "%~dp0Start SWhereUsed.bat.new" "%~f0" >nul & "%~f0" --no-browser
goto run


:findpython
rem Prefer the Python launcher (py), then python on the PATH. Both must be 3.9+ and 64-bit.
rem The "python" that only opens the Microsoft Store fails this check and is skipped.
set "PY="
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 9) and sys.maxsize > 2**32 else 1)"
py -3 -c "%CHECK%" >nul 2>nul
if not errorlevel 1 (set "PY=py -3" & exit /b)
python -c "%CHECK%" >nul 2>nul
if not errorlevel 1 (set "PY=python" & exit /b)
exit /b


:notunzipped
echo  SWhereUsed is started from inside the zip file.
echo  Unzip the whole SWhereUsed folder first (right-click the zip, Extract All...),
echo  then start "Start SWhereUsed.bat" from the unzipped folder.
echo.
pause
exit /b 1

:nopython
echo.
echo  No 64-bit Python 3.9 or newer was found on this pc.
echo.
where winget >nul 2>nul
if errorlevel 1 goto manual
choice /c YN /m " Install Python 3.12 for your user account now (no administrator rights needed)"
if errorlevel 2 goto manual
winget install --exact --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto manual
echo.
echo  Python is installed. Close this window and start SWhereUsed again.
pause
exit /b 1

:manual
echo  Install Python from https://www.python.org/downloads/windows/
echo  (64-bit, and tick "Add python.exe to PATH"), then start SWhereUsed again.
pause
exit /b 1

:pipfail
echo.
echo  Installing pywin32 and comtypes failed, see the messages above.
echo  Is this pc behind a proxy or offline? Ask IT to allow pypi.org and files.pythonhosted.org.
pause
exit /b 1

:fail
echo.
echo  Creating the Python environment failed, see the messages above.
pause
exit /b 1
