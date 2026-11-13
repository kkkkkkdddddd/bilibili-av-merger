@echo off
setlocal
cd /d "%~dp0"
if exist "C:\ProgramData\anaconda3\pythonw.exe" (
    start "" "C:\ProgramData\anaconda3\pythonw.exe" -B "%~dp0merge.pyw"
    exit /b
)
where pyw.exe >nul 2>&1
if not errorlevel 1 (
    start "" pyw.exe -3 -B "%~dp0merge.pyw"
    exit /b
)
echo Python with Tkinter was not found.
pause
