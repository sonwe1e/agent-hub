@echo off
rem Keep this file ASCII-only: cmd.exe uses the system OEM codepage.
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"

rem The packaged app needs neither Python on PATH nor a web server.
if exist "%ROOT%dist\AgentHub.exe" goto packaged

rem Source fallback: reject Store aliases and interpreters without the GUI deps.
set "PY="
if defined MIMO_PYTHON call :probe "%MIMO_PYTHON%"
if not defined PY call :probe "%ROOT%.venv-build\Scripts\python.exe"
if not defined PY call :probe "%ROOT%.venv\Scripts\python.exe"
if not defined PY for %%I in (python.exe) do call :probe "%%~$PATH:I"
if not defined PY for /d %%P in ("%LOCALAPPDATA%\Programs\Python\*") do call :probe "%%~P\python.exe"
if not defined PY for /d %%P in ("%ProgramFiles%\Python*") do call :probe "%%~P\python.exe"
if not defined PY for /d %%P in ("C:\Python*") do call :probe "%%~P\python.exe"
if not defined PY for /d %%R in ("%USERPROFILE%\.dsh\dsh-runtimes\*") do call :probe "%%~R\dependencies\python\python.exe"
if not defined PY (
  echo [Agent Hub] Desktop runtime not found.
  echo Build dist\AgentHub.exe or install requirements-desktop.txt with Python 3.11+.
  echo Set MIMO_PYTHON to that python.exe if it is not discovered automatically.
  exit /b 1
)

rem Diagnostic arguments retain stdout; ordinary launches have no console.
set "PYW=%PY%"
if "%~1"=="" for %%I in ("%PY%") do if exist "%%~dpIpythonw.exe" set "PYW=%%~dpIpythonw.exe"
"%PYW%" "%ROOT%desktop_app.py" %*
exit /b %ERRORLEVEL%

:packaged
start "" /wait "%ROOT%dist\AgentHub.exe" %*
exit /b %ERRORLEVEL%

:probe
if defined PY exit /b 0
if "%~1"=="" exit /b 0
if not exist "%~1" exit /b 0
rem A distinct code proves Python ran the probe; some Store aliases return 0.
"%~1" -c "import sys, webview, pystray, PIL; assert sys.version_info >= (3, 11); sys.exit(42)" >nul 2>&1
if not "%ERRORLEVEL%"=="42" exit /b 0
set "PY=%~1"
exit /b 0
