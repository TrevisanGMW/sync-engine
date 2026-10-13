@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "SYNCENGINE_SCRIPT=%~dp0..\sync_engine\sync_engine.py"
set "SYNCENGINE_LAUNCHER=%~dp0..\sync_engine\launch_sync_engine.pyw"
set "SYNCENGINE_ARGS=%*"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist "%SYNCENGINE_SCRIPT%" (
    echo SyncEngine could not find its application package:
    echo "%SYNCENGINE_SCRIPT%"
    pause
    exit /b 1
)
if not exist "%SYNCENGINE_LAUNCHER%" (
    echo SyncEngine could not find its Python launcher:
    echo "%SYNCENGINE_LAUNCHER%"
    pause
    exit /b 1
)

:check_arguments
if "%~1"=="" goto arguments_checked
if /I "%~1"=="--headless" goto headless
if /I "%~1"=="--preview" goto headless
if /I "%~1"=="--help" goto headless
if /I "%~1"=="-h" goto headless
shift
goto check_arguments

:arguments_checked

if not defined SYNCENGINE_PYTHON set "SYNCENGINE_PYTHON=%SYNCTASKER_PYTHON%"
if not defined SYNCENGINE_PYTHON set "SYNCENGINE_PYTHON=%FOLDER_RELAY_PYTHON%"
set "SYNCENGINE_PYTHON=%SYNCENGINE_PYTHON%"
if defined SYNCENGINE_PYTHON goto gui_override
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%SYNCENGINE_LAUNCHER%" %SYNCENGINE_ARGS%
    exit /b
)
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%SYNCENGINE_LAUNCHER%" %SYNCENGINE_ARGS%
    exit /b
)
goto find_python

:gui_override
for %%I in ("%SYNCENGINE_PYTHON%") do set "SYNCENGINE_EXE=%%~dpnI"
for %%I in ("%SYNCENGINE_PYTHON%") do set "SYNCENGINE_EXE_NAME=%%~nxI"
if /I "%SYNCENGINE_EXE_NAME%"=="pythonw.exe" (
    start "" "%SYNCENGINE_PYTHON%" "%SYNCENGINE_LAUNCHER%" %SYNCENGINE_ARGS%
    exit /b
)
if /I "%SYNCENGINE_EXE_NAME%"=="pyw.exe" (
    start "" "%SYNCENGINE_PYTHON%" -3 "%SYNCENGINE_LAUNCHER%" %SYNCENGINE_ARGS%
    exit /b
)
if exist "%SYNCENGINE_EXE%w.exe" (
    start "" "%SYNCENGINE_EXE%w.exe" "%SYNCENGINE_LAUNCHER%" %SYNCENGINE_ARGS%
    exit /b
)
start "" "%SYNCENGINE_PYTHON%" "%SYNCENGINE_LAUNCHER%" %SYNCENGINE_ARGS%
exit /b

:find_python
where python.exe >nul 2>nul
if not errorlevel 1 (
    set "SYNCENGINE_PYTHON=python.exe"
    goto gui_run
)
where py.exe >nul 2>nul
if not errorlevel 1 (
    py.exe -3 "%SYNCENGINE_LAUNCHER%" %SYNCENGINE_ARGS%
    exit /b
)
for /d %%D in ("%ProgramFiles%\Autodesk\Maya*") do (
    if exist "%%D\bin\mayapy.exe" set "SYNCENGINE_PYTHON=%%D\bin\mayapy.exe"
)
if defined SYNCENGINE_PYTHON goto gui_run
echo SyncEngine needs an existing Python interpreter.
echo The Python launcher will install its graphical requirements into a private runtime on first use.
pause
exit /b 1

:gui_run
"%SYNCENGINE_PYTHON%" "%SYNCENGINE_LAUNCHER%" %SYNCENGINE_ARGS%
exit /b %errorlevel%

:headless
if not defined SYNCENGINE_PYTHON set "SYNCENGINE_PYTHON=%SYNCTASKER_PYTHON%"
if not defined SYNCENGINE_PYTHON set "SYNCENGINE_PYTHON=%FOLDER_RELAY_PYTHON%"
set "SYNCENGINE_PYTHON=%SYNCENGINE_PYTHON%"
if not defined SYNCENGINE_PYTHON (
    where python.exe >nul 2>nul
    if not errorlevel 1 set "SYNCENGINE_PYTHON=python.exe"
)
if not defined SYNCENGINE_PYTHON (
    where py.exe >nul 2>nul
    if not errorlevel 1 (
        py.exe -3 "%SYNCENGINE_SCRIPT%" %SYNCENGINE_ARGS%
        exit /b
    )
)
if not defined SYNCENGINE_PYTHON (
    for /d %%D in ("%ProgramFiles%\Autodesk\Maya*") do (
        if exist "%%D\bin\mayapy.exe" set "SYNCENGINE_PYTHON=%%D\bin\mayapy.exe"
    )
)
if not defined SYNCENGINE_PYTHON goto no_python
for %%I in ("%SYNCENGINE_PYTHON%") do set "SYNCENGINE_EXE=%%~dpnI"
for %%I in ("%SYNCENGINE_PYTHON%") do set "SYNCENGINE_EXE_NAME=%%~nxI"
if /I "%SYNCENGINE_EXE_NAME%"=="pythonw.exe" set "SYNCENGINE_PYTHON=%SYNCENGINE_EXE%.exe"
if /I "%SYNCENGINE_EXE_NAME%"=="pyw.exe" set "SYNCENGINE_PYTHON=%SYNCENGINE_EXE%.exe"

:console_run
"%SYNCENGINE_PYTHON%" "%SYNCENGINE_SCRIPT%" %SYNCENGINE_ARGS%
exit /b %errorlevel%

:no_python
echo SyncEngine needs an existing Python interpreter.
pause
exit /b 1
