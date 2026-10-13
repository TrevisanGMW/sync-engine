@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "APP_DIR=%%~fI"
cd /d "%APP_DIR%"
for %%I in ("%~dp0build") do set "BUILD_DIR=%%~fI"
for %%I in ("%~dp0dist") do set "DIST_DIR=%%~fI"
set "RESOURCE_DIR=%APP_DIR%\sync_engine\resources"

if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"
if not exist "%DIST_DIR%" mkdir "%DIST_DIR%"

python -c "import PyInstaller, PySide6" >nul 2>nul
if errorlevel 1 (
    echo SyncEngine EXE builds require PySide6 and PyInstaller in the active Python environment.
    echo Install them with: python -m pip install PySide6 PyInstaller
    pause
    exit /b 1
)

python -m PyInstaller --noconfirm --clean --onefile --windowed --name SyncEngine --icon "%RESOURCE_DIR%\sync_engine.ico" --distpath "%DIST_DIR%" --workpath "%BUILD_DIR%\work" --specpath "%BUILD_DIR%" --add-data "%RESOURCE_DIR%;sync_engine\resources" "%APP_DIR%\sync_engine\sync_engine.py"
if errorlevel 1 (
    echo The EXE build failed. Review the output above for details.
    pause
    exit /b 1
)

echo Created scripts\dist\SyncEngine.exe
pause
