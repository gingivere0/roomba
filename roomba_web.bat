@echo off
rem Starts the Roomba web app with phone access on your home Wi-Fi.
rem Remove --lan from the next line to keep it reachable from this PC only.
"%~dp0.venv\Scripts\python.exe" "%~dp0roomba_web.py" --lan %*
pause
