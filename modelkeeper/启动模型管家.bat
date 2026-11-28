@echo off
setlocal
set "HERE=%~dp0"
rem 只用 PATH 里的解释器，保证换台机器也能双击起来（不要把本机绝对路径写进来）
set "PYW="
where pythonw >nul 2>nul && set "PYW=pythonw"
if not defined PYW (
  where py >nul 2>nul && set "PYW=py -w"
)
if not defined PYW (
  echo 未找到 pythonw：请安装 Python 3.12 或更高版本，并在安装时勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)
start "" %PYW% "%HERE%main.py"
