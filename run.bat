@echo off
rem Start Meridian on http://127.0.0.1:8790/ and open the browser. Close this window to stop it.
cd /d "%~dp0"
python server.py --open %*
