@echo off
rem Build or refresh every dataset now (the server also does this in the background).
cd /d "%~dp0"
python server.py --build all %*
pause
