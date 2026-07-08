@echo off
setlocal

set "BUNDLED_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if not exist "%BUNDLED_PY%" goto check_python
"%BUNDLED_PY%" "%~dp0zhuorui_market_order.py" %*
exit /b %ERRORLEVEL%

:check_python
where python >nul 2>nul
if not %ERRORLEVEL%==0 goto check_py
python "%~dp0zhuorui_market_order.py" %*
exit /b %ERRORLEVEL%

:check_py
where py >nul 2>nul
if not %ERRORLEVEL%==0 goto no_python
py "%~dp0zhuorui_market_order.py" %*
exit /b %ERRORLEVEL%

:no_python
echo Could not find Python. Install Python or run with the bundled Codex Python path. 1>&2
exit /b 2
