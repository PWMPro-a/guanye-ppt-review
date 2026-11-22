@echo off
setlocal
chcp 65001 >nul

set "PWSH="
if exist "C:\Users\ylk\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe" set "PWSH=C:\Users\ylk\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe"
if not defined PWSH if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PWSH for /f "delims=" %%P in ('where pwsh.exe 2^>nul') do if not defined PWSH set "PWSH=%%P"
if not defined PWSH goto :no_pwsh

"%PWSH%" -NoLogo -NoProfile -Command "if ($PSVersionTable.PSVersion.Major -lt 7) { exit 9 }" >nul 2>&1
if errorlevel 1 goto :bad_pwsh

"%PWSH%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%

:no_pwsh
echo [ERROR] PowerShell 7 was not found. Install PowerShell 7 and try again.
pause
exit /b 9009

:bad_pwsh
echo [ERROR] The detected pwsh.exe is not PowerShell 7: %PWSH%
pause
exit /b 9008