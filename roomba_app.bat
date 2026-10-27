@echo off
rem Starts the new Roomba app (looks like the official iRobot app).
rem The older app is still roomba_web.bat.
rem When an update arrives the app exits with code 3 and starts again here.
:run
"%~dp0.venv\Scripts\python.exe" "%~dp0app\server.py" %*
if %errorlevel%==3 (
  echo.
  goto run
)
pause
