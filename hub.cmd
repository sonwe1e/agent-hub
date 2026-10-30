@echo off
rem Agent Hub command entry point.
rem On this machine PATH python.exe is a 0-byte Microsoft Store stub that exits
rem with 9009, so probe for a real interpreter instead of trusting PATH.
rem NOTE: keep this file ASCII-only - cmd.exe parses it in the OEM codepage.
setlocal EnableExtensions
set "ROOT=%~dp0"
if /i "%~1"=="gui" goto desktop
if /i "%~1"=="desktop" goto desktop
set "PY="

rem 1) explicit override
if defined MIMO_PYTHON if exist "%MIMO_PYTHON%" set "PY=%MIMO_PYTHON%"

rem 2) python.exe from PATH (the :probe syntax check rejects the stub)
if not defined PY for %%I in (python.exe) do call :probe "%%~$PATH:I"

rem 3) common install locations
if not defined PY for /d %%P in ("%LOCALAPPDATA%\Programs\Python\*") do call :probe "%%~P\python.exe"
if not defined PY for /d %%P in ("%ProgramFiles%\Python*") do call :probe "%%~P\python.exe"
if not defined PY for /d %%P in ("C:\Python*") do call :probe "%%~P\python.exe"

rem 4) Python bundled with the DSH runtime
if not defined PY for /d %%R in ("%USERPROFILE%\.dsh\dsh-runtimes\*") do call :probe "%%~R\dependencies\python\python.exe"

if not defined PY (
  echo [Agent Hub] No usable Python interpreter found.
  echo   Install Python 3, or point MIMO_PYTHON at python.exe, e.g.
  echo   setx MIMO_PYTHON "C:\Python312\python.exe"
  exit /b 1
)

rem Prefer the windowless interpreter for background GUI launches.
set "PYW=%PY%"
for %%I in ("%PY%") do if exist "%%~dpIpythonw.exe" set "PYW=%%~dpIpythonw.exe"

if "%~1"=="ui" (
  start "" "%PYW%" "%ROOT%launch.py" %*
  exit /b 0
)
if "%~1"=="gui-legacy" (
  start "" "%PYW%" "%ROOT%gui.py"
  exit /b 0
)
if "%~1"=="serve" (
  start "" "%PYW%" "%ROOT%launch.py" %*
  exit /b 0
)
"%PY%" "%ROOT%hub.py" %*
exit /b %ERRORLEVEL%

:desktop
call "%ROOT%desktop.cmd"
exit /b %ERRORLEVEL%

:probe
rem Accept only an interpreter that actually runs (Store stub returns non-zero).
if "%~1"=="" exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import sys" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%~1"
exit /b 0
