@echo off
rem Starts Notes, opens it, and shows a QR code so your phone can connect.
rem Put a shortcut to this file on your desktop. Keep the window open while you
rem use Notes: closing it stops the app.
title Notes
mode con cols=72 lines=44
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python wasn't found on this computer.
    echo Install Python 3.10 or newer from python.org, then try again.
    echo.
    pause
    exit /b 1
)

rem Already running? Open it and show the QR code again.
netstat -an | find ":7777 " | find "LISTENING" >nul
if not errorlevel 1 (
    start "" "http://127.0.0.1:7777"
    python -m mnemo connect
    echo.
    pause
    exit /b 0
)

echo Starting Notes...
echo Keep this window open. Closing it stops Notes.
echo.
python -m mnemo serve --lan

echo.
echo Notes has stopped.
pause
