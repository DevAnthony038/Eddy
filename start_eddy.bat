@echo off
setlocal
title Eddy

cd /d "%~dp0"

rem ---------- Python ----------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3 was not found.
  echo Install it from https://www.python.org/downloads/ and run this again.
  echo.
  pause
  exit /b 1
)

rem ---------- Ollama ----------
where ollama >nul 2>nul
if errorlevel 1 (
  echo Ollama was not found.
  echo Install it from https://ollama.com/download and run this again.
  echo.
  pause
  exit /b 1
)

rem ---------- Python dependencies ----------
%PY% -m pip install -r requirements.txt >nul
if errorlevel 1 (
  echo Could not install Python dependencies.
  echo Run this to see the error:
  echo     %PY% -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

rem ---------- Run ----------
%PY% server.py
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
  echo.
  echo Eddy stopped with an error (exit code %CODE%). See the message above.
  echo.
  pause
)

endlocal
exit /b %CODE%