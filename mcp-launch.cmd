@echo off
REM Portable launcher for hantek-bench MCP.
setlocal EnableExtensions
set "ROOT=%~dp0"
set "PY="

where py >nul 2>&1 && (
  for /f "delims=" %%I in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%I"
)
if not defined PY if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
if not defined PY if exist "C:\Program Files\Python314\python.exe" set "PY=C:\Program Files\Python314\python.exe"
if not defined PY (
  where python >nul 2>&1 && for /f "delims=" %%I in ('where python') do (
    set "PY=%%I"
    goto :have_py
  )
)
:have_py
if not defined PY (
  echo hantek-bench: no Python found. Install Python 3.12+ >&2
  exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONPATH=%ROOT%"
if not defined HTK_ARTIFACT_DIR set "HTK_ARTIFACT_DIR=%LocalAppData%\hantek-bench\captures"
if not defined HTK_SIGROK_CLI if exist "%LocalAppData%\hantek-bench\sigrok-cli\sigrok-cli.exe" set "HTK_SIGROK_CLI=%LocalAppData%\hantek-bench\sigrok-cli\sigrok-cli.exe"
if not defined HTK_SIGROK_CLI if exist "%LocalAppData%\BMI-Hersom\sigrok-cli\sigrok-cli.exe" set "HTK_SIGROK_CLI=%LocalAppData%\BMI-Hersom\sigrok-cli\sigrok-cli.exe"

"%PY%" -c "import mcp,numpy,fastapi" 1>nul 2>nul
if errorlevel 1 (
  echo hantek-bench: installing requirements into "%PY%" >&2
  "%PY%" -m pip install --user -r "%ROOT%requirements.txt"
  if errorlevel 1 exit /b 1
)

"%PY%" -m hantek_navigator.mcp_server
