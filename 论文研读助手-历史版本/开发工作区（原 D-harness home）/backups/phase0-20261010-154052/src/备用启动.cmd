@echo off
cd /d "%~dp0"
if exist "C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe" (
  start "" "C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe" "%~dp0launch.py"
  exit /b
)
py -3 "%~dp0launch.py"
if errorlevel 1 python "%~dp0launch.py"
if errorlevel 1 pause
