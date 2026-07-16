@echo off
REM ============================================================
REM  TriageIQ (Bugs to Quality Coverage) - one-click launcher
REM  Double-click this file to set up and start the app.
REM ============================================================
setlocal
cd /d "%~dp0webapp"

where python >nul 2>&1
if errorlevel 1 (
  echo [X] Python was not found.
  echo     Install Python 3.9+ from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^),
  echo     then double-click start.bat again.
  pause
  exit /b 1
)

if not exist "config.env" (
  copy "config.env.example" "config.env" >nul
  echo [i] Created config.env - opening it now.
  echo     Paste your 4 tokens ^(Atlassian email + API token, Xray client id + secret^), SAVE, close Notepad,
  echo     then double-click start.bat again.
  notepad "config.env"
  pause
  exit /b 0
)

findstr /C:"PASTE_" "config.env" >nul
if not errorlevel 1 (
  echo [X] config.env still has PASTE_ placeholders.
  echo     Open  webapp\config.env , fill in your 4 tokens, save, and run start.bat again.
  pause
  exit /b 1
)

echo [i] Starting TriageIQ...
echo     When you see  "URL : http://localhost:8756"  below, open that link in your browser.
echo     ^(The first run can take a minute to fetch the code-RCA source.^)
echo.
python triage_server.py
pause
