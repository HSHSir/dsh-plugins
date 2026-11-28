@echo off
setlocal
rem ModelKeeper - one-click packaging (PyInstaller onefile, no console, custom icon)
rem
rem 1) Pick a Python that actually has tkinter. PyInstaller cannot invent Tcl/Tk:
rem    an exe built with a tk-less interpreter exits the moment it starts. On this
rem    machine PATH `python` is a slim 3.13 without tk, while `py -3.14` has it.
rem 2) Then hand over to scripts\build_exe.py, which does the real work (version bump,
rem    version_info.txt, PyInstaller). Extra args are passed through, e.g.
rem        packaging --no-bump
rem
rem No CJK literals below: a .bat is decoded with the console code page (936/65001),
rem so Chinese text here breaks whenever the code page changes.
cd /d "%~dp0"

set "PY="
for %%V in (3.14 3.13 3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import tkinter" >nul 2>&1
    if not errorlevel 1 set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import tkinter" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  python3 -c "import tkinter" >nul 2>&1
  if not errorlevel 1 set "PY=python3"
)

if not defined PY (
  echo [error] No Python with tkinter found.
  echo         PyInstaller cannot add Tcl/Tk for you: an exe built without tk
  echo         starts and vanishes immediately.
  echo         Install the official build from python.org, or run: py -3.14 scripts\build_exe.py
  if not defined MK_NO_PAUSE pause
  exit /b 1
)

echo [build] using %PY%
%PY% scripts\build_exe.py %*
if errorlevel 1 (
  echo [failed] see the error above
  if not defined MK_NO_PAUSE pause
  exit /b 1
)

if not defined MK_NO_PAUSE pause
