@echo off
setlocal
title Quant Trading
cd /d "%~dp0"

echo ==================================================
echo    Quant Trading  [launcher]
echo ==================================================
echo.

REM ---------------------------------------------------------------- Node.js
where node >nul 2>&1
if not errorlevel 1 goto node_ok
echo [..] Node.js not found - installing automatically via winget...
echo     (you may see a Windows permission prompt - click Yes)
where winget >nul 2>&1
if errorlevel 1 goto no_winget_node
winget install -e --id OpenJS.NodeJS.LTS --silent --accept-source-agreements --accept-package-agreements
call :reload_path
if exist "%ProgramFiles%\nodejs\node.exe" set "PATH=%ProgramFiles%\nodejs;%PATH%"
where node >nul 2>&1
if errorlevel 1 goto node_restart
:node_ok
for /f "delims=" %%v in ('node --version') do set "NODEV=%%v"
echo [ok] Node.js %NODEV%
REM Node 22+ can trust the OS certificate store (needed behind a corporate SSL
REM proxy). SYSCA is used only if the normal install fails.
for /f "tokens=1 delims=." %%a in ("%NODEV:v=%") do set "NODEMAJOR=%%a"
set "SYSCA="
if %NODEMAJOR% GEQ 22 set "SYSCA=--use-system-ca"

where npm >nul 2>&1
if errorlevel 1 goto no_npm
for /f "delims=" %%v in ('npm --version') do set "NPMV=%%v"
echo [ok] npm  %NPMV%
echo.

REM ------------------------------------------- Python + blpapi (live updater)
call :find_python
if defined QPY goto have_py
echo [..] Python not found - installing automatically via winget...
echo     (you may see a Windows permission prompt - click Yes)
where winget >nul 2>&1
if errorlevel 1 goto py_manual
winget install -e --id Python.Python.3.12 --silent --scope user --accept-source-agreements --accept-package-agreements
call :reload_path
call :find_python
if not defined QPY goto py_restart
:have_py
set "QUANT_PYTHON=%QPY%"
echo [ok] Python: %QPY%

REM --- Core Python packages (pandas/numpy/exchange_calendars) --------------
REM The backtest, live signal and updater import these. The app's charts read
REM quant.db directly and don't need them, but the Python side won't run without
REM them (ModuleNotFoundError: No module named 'pandas').
"%QPY%" -c "import pandas, numpy, exchange_calendars" >nul 2>&1
if not errorlevel 1 goto pydeps_ok
REM Install OFFLINE from the wheels bundled in engine\vendor\wheels (no network,
REM so a corporate proxy / VPN / antivirus HTTPS scanning can't block it). The
REM bundle covers 64-bit Windows Python 3.11 / 3.12 / 3.13.
echo [..] Installing Python packages from the bundled offline wheels...
"%QPY%" -m pip install --no-index --find-links "%~dp0engine\vendor\wheels" -r "%~dp0engine\requirements.txt"
if not errorlevel 1 goto pydeps_ok
REM No matching bundled wheel (unusual Python version/platform) - fall back to a
REM normal online install, then to SSL-proxy-tolerant retries.
echo [..] Bundled wheels did not match this Python; trying an online install...
"%QPY%" -m pip install -r "%~dp0engine\requirements.txt"
if not errorlevel 1 goto pydeps_ok
echo [..] Retrying using the Windows certificate store...
"%QPY%" -m pip install --use-feature=truststore -r "%~dp0engine\requirements.txt"
if not errorlevel 1 goto pydeps_ok
echo [..] Retrying with the PyPI hosts trusted...
"%QPY%" -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org -r "%~dp0engine\requirements.txt"
if errorlevel 1 (
echo [!] Could not install Python packages ^(pandas/numpy/exchange_calendars^).
echo     The bundled wheels cover 64-bit Windows Python 3.11/3.12/3.13; this PC
echo     may have a different Python. The backtest and live signal need these
echo     packages; the app's charts work without them.
)
:pydeps_ok
echo [ok] Python packages ready.

"%QPY%" -c "import blpapi" >nul 2>&1
if not errorlevel 1 goto blpapi_ok
echo [..] Installing blpapi automatically (Bloomberg package index)...
"%QPY%" -m pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
if errorlevel 1 echo [!] blpapi install failed - charts still work; it will retry next launch.
goto py_done
:blpapi_ok
echo [ok] blpapi installed - the live updater is ready.
goto py_done
:py_manual
echo [i] No Python and winget is unavailable. Install Python 3 from python.org,
echo     then re-run. Charts work without it; the updater needs it.
goto py_done
:py_restart
echo [i] Python was installed but this window cannot see it yet. CLOSE this
echo     window and re-run run.bat to finish updater setup. Charts still work.
:py_done
echo.

REM --------------------------------------------------------- App dependencies
cd /d "%~dp0app"
if not exist "package.json" goto no_app
if exist "node_modules\.bin\electron.cmd" goto have_deps
echo [..] Installing app dependencies. First run only - this downloads
echo      Electron and can take a few minutes...
echo.
call npm install
if not errorlevel 1 goto deps_check
REM Install failed. Behind a corporate SSL proxy, Electron's download fails with
REM "self-signed certificate in certificate chain". Retry trusting the Windows
REM certificate store (Node 22+) -- this uses IT-installed certs and does NOT
REM disable TLS verification.
if not defined SYSCA goto npm_fail
echo.
echo [..] Retrying using the Windows certificate store...
set "NODE_OPTIONS=%SYSCA%"
call npm install
set "NODE_OPTIONS="
:deps_check
REM Electron can also be skipped as a devDependency in "production" mode; if it's
REM missing, install it explicitly (also via the system cert store where needed).
if exist "node_modules\.bin\electron.cmd" goto have_deps
echo [..] Installing Electron explicitly...
set "NODE_OPTIONS=%SYSCA%"
call npm install electron@31.7.7
set "NODE_OPTIONS="
if not exist "node_modules\.bin\electron.cmd" goto electron_fail
:have_deps
echo [ok] App dependencies ready.
:launch
echo.
echo [^>^>] Launching the viewer...
echo.
REM Launch via Electron's own binary (avoids PATH issues with `npm start`).
call "node_modules\.bin\electron.cmd" .
if errorlevel 1 goto app_fail
goto done

:electron_fail
echo.
echo [!] Electron could not be installed. Check your internet connection and that
echo     npm is not blocked, then run this file again.
goto done

REM ------------------------------------------------------------------- Errors
:no_winget_node
echo [!] Node.js is missing and winget is unavailable. Install Node.js LTS from
echo        https://nodejs.org/en/download
echo     then run this file again.
goto done

:node_restart
echo [i] Node.js was installed but this window cannot see it yet.
echo     CLOSE this window and double-click run.bat again.
goto done

:no_npm
echo [!] npm was not found - it normally ships with Node.js. Reinstall Node.js.
goto done

:no_app
echo [!] app\package.json is missing - the app folder looks incomplete.
goto done

:npm_fail
echo.
echo [!] npm install failed. Check your internet connection and try again.
goto done

:app_fail
echo.
echo [!] The app exited with an error - see the messages above.
goto done

:done
echo.
pause
exit /b

REM ----------------------------------------------------- Subroutines (below)

REM Reload PATH from the registry so tools installed moments ago are visible
REM without needing to reopen the window. Guarded: only overwrites on success.
:reload_path
set "MPATH="
set "UPATH="
for /f "skip=2 tokens=2,*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "MPATH=%%b"
for /f "skip=2 tokens=2,*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "UPATH=%%b"
if not defined MPATH exit /b
if defined UPATH set "PATH=%MPATH%;%UPATH%"
if not defined UPATH set "PATH=%MPATH%"
exit /b

REM Set QPY to the first Python found: QUANT_PYTHON override, a conda 'bloomberg'
REM env under the user profile, the 'py' launcher, or python on PATH.
:find_python
set "QPY="
if defined QUANT_PYTHON set "QPY=%QUANT_PYTHON%"
if defined QPY exit /b
if exist "%USERPROFILE%\anaconda3\envs\bloomberg\python.exe" set "QPY=%USERPROFILE%\anaconda3\envs\bloomberg\python.exe"
if defined QPY exit /b
if exist "%USERPROFILE%\miniconda3\envs\bloomberg\python.exe" set "QPY=%USERPROFILE%\miniconda3\envs\bloomberg\python.exe"
if defined QPY exit /b
where py >nul 2>&1
if not errorlevel 1 set "QPY=py"
if defined QPY exit /b
REM Take the first python on PATH, EXCLUDING the Microsoft Store execution-alias
REM stub (a fake WindowsApps\python.exe that just opens the Store).
for /f "delims=" %%p in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined QPY set "QPY=%%p"
exit /b
