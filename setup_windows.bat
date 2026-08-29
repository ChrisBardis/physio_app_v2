@echo off
cd /d %~dp0
if not exist .venv (
  py -3.13 -m venv .venv
  if errorlevel 1 py -m venv .venv
  if errorlevel 1 python -m venv .venv
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
if errorlevel 1 exit /b 1
echo Η εγκατάσταση ολοκληρώθηκε.
