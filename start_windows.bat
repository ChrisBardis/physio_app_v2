@echo off
cd /d %~dp0
if not exist .venv (
  call setup_windows.bat
  if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe run.py
pause
