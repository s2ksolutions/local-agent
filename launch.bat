@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
 py -3 -m venv .venv
 if errorlevel 1 goto fail
 .venv\Scripts\python.exe -m pip install -r requirements.txt
 if errorlevel 1 goto fail
)
.venv\Scripts\python.exe -c "from runtime import Runtime,validate_flow,TOOLS,groups"
if errorlevel 1 goto fail
start "" http://127.0.0.1:8765
.venv\Scripts\python.exe app.py
pause
exit /b
:fail
echo Setup or import check failed. See the error above. Python 3.10+ is required.
pause
