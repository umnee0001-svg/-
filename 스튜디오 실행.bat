@echo off
chcp 65001 > nul
cd /d "%~dp0"
where python > nul 2> nul
if errorlevel 1 (
  echo Python 이 설치되어 있지 않습니다. https://www.python.org 에서 설치할 때 "Add python.exe to PATH" 를 체크하세요.
  pause
  exit /b 1
)
where ffmpeg > nul 2> nul
if errorlevel 1 (
  echo ffmpeg 가 없습니다. 명령 프롬프트에서  winget install Gyan.FFmpeg  을 실행한 뒤 다시 켜 주세요.
  pause
  exit /b 1
)
python src\studio.py
pause
