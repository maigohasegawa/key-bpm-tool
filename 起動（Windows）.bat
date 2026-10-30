@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 key_bpm_app.py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python key_bpm_app.py
  ) else (
    echo Python 3 が見つかりませんでした。https://www.python.org/downloads/ からインストールしてください。
  )
)
pause
