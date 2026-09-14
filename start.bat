@echo off
rem Launch the power monitor without leaving a console window behind.
setlocal

set "PYW=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"

if exist "%PYW%" goto run

rem Fallback: whatever pythonw happens to be on PATH
for %%I in (pythonw.exe) do set "PYW=%%~$PATH:I"
if not "%PYW%"=="" goto run

echo Cannot find pythonw.exe. Set PYW manually in this file.
pause
exit /b 1

:run
start "" "%PYW%" "%~dp0run.pyw"
exit /b 0
