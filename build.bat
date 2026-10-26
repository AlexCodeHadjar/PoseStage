@echo off
setlocal
cd /d "%~dp0"
if defined POSESTAGE_PYTHON (
  set "PYTHON=%POSESTAGE_PYTHON%"
) else if exist ".venv\Scripts\python.exe" (
  set "PYTHON=%~dp0.venv\Scripts\python.exe"
) else (
  set "PYTHON=python"
)
"%PYTHON%" -c "import sys; assert sys.version_info[:2] == (3,12), 'Python 3.12 required'"
if errorlevel 1 goto fail
"%PYTHON%" -m PyInstaller --noconfirm --clean --onefile --windowed --name PoseStage --icon icon.ico --add-data "web;web" --add-data "parts_analyzer.py;." --add-data "figure_doc.py;." --add-data "rig.py;." --add-data "rig_doc.py;." --add-data "timeline.py;." --hidden-import parts_service --hidden-import agent_bridge app.py
if errorlevel 1 goto fail
echo Built: %~dp0dist\PoseStage.exe
exit /b 0
:fail
echo Build failed. See output above.
exit /b 1
