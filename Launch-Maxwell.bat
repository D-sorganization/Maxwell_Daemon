@echo off
title Launching Maxwell-Daemon
echo ==================================================
echo   Booting Maxwell-Daemon Desktop Environment...
echo ==================================================
set PYTHONPATH=%cd%

REM Check if virtual environment exists
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found at .venv\Scripts\activate.bat
    echo Please run setup first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

REM Start the PyQt6 GUI as a background windowless process
start pythonw -m maxwell_daemon.gui.app
exit
