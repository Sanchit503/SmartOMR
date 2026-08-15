@echo off
REM SmartOMR — same generator, as a desktop form instead of terminal prompts.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Virtual environment not found. Run this once first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m omr.generator.main --gui
