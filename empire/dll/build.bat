@echo off
rem Baut empire_bridge.dll (32-Bit, MSVC). Voraussetzung: Visual Studio 2019/2022 oder die Build Tools
rem mit "Desktopentwicklung mit C++" (MSVC x86/x64-Buildtools).
setlocal
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
  echo FEHLER: vswhere.exe nicht gefunden - Visual Studio bzw. die Build Tools sind nicht installiert.
  exit /b 1
)
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSDIR=%%i"
if not defined VSDIR (
  echo FEHLER: keine Visual-Studio-Installation mit C++-Buildtools gefunden.
  exit /b 1
)
call "%VSDIR%\VC\Auxiliary\Build\vcvars32.bat" >nul || exit /b 1
cd /d "%~dp0"
cl /nologo /LD /O2 /MT /W3 /D_CRT_SECURE_NO_WARNINGS empire_bridge.c overlay.c /link /DLL /OUT:empire_bridge.dll kernel32.lib user32.lib gdi32.lib || exit /b 1
del /q *.obj *.exp *.lib 2>nul
echo OK: %~dp0empire_bridge.dll gebaut. Installieren: python install.py
