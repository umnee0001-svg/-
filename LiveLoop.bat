@echo off
title LiveLoop - 24h live
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 src\live_app.py %*
) else (
  python src\live_app.py %*
)
if errorlevel 1 pause
