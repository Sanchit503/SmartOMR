@echo off
REM SmartOMR — OMR sheet generator (interactive wizard).
REM Double-click to run: it asks for the exam requirements, then writes the
REM printable PDF + template manifest into data\exams and opens the PDF.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Run this once first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m omr.generator.main %*
echo.
pause
