@echo off
cd /d "%~dp0"
python 源码\main.py %*
if errorlevel 1 pause
