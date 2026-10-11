@echo off
setlocal
rem Usa o Python do ambiente da instalacao (install.py / npx); fora dela, o Python do PATH.
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=python"
if not exist "%PYW%" set "PYW=pythonw"
if "%~1"=="" goto gui
rem Sem pause aqui: o launcher.py so espera ENTER quando o console foi aberto so para ele (duplo clique, atalho).
"%PY%" "%~dp0launcher.py" %*
exit /b %errorlevel%

:gui
start "" "%PYW%" "%~dp0launcher.pyw"
exit /b 0
