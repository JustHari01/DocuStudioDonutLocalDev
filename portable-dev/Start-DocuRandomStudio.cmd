@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-DocuRandomStudio-Background.ps1"
if errorlevel 1 (
  echo.
  echo Khong the khoi dong bo DocuRandom portable. Xem loi o tren.
  pause
)
endlocal
