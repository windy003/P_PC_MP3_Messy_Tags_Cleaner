@echo off
rem Double-click launcher for mp3_tag_cleaner.py
cd /d "%~dp0"
python "%~dp0mp3_tag_cleaner.py" %*
if errorlevel 1 pause
