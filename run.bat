@echo off
cd /d "%~dp0"
python app\main.py %*
if errorlevel 1 pause
