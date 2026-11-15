@echo off
chcp 65001 >nul 2>nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ==========================================================
echo   BYD Monitor / Multi-Symbol Board - Windows one-click
echo ==========================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.9+ and add it to PATH.
  echo         https://www.python.org/downloads/
  pause
  exit /b 1
)
for /f "tokens=2" %%v in ('python -V 2^>^&1') do echo [OK] Python %%v

echo [1/4] Syntax check ...
python -m py_compile byd_monitor.py byd_web_server.py portfolio.py
if errorlevel 1 (
  echo [ERROR] Python syntax check failed.
  pause
  exit /b 1
)

echo [2/4] Fetching 17 ETF / stocks (incl. BYD) and building the report ...
python portfolio.py --once --workers 6
if errorlevel 1 (
  echo [ERROR] Data fetch failed. Check network access to quote.eastmoney.com.
  pause
  exit /b 1
)

echo [3/4] Opening the standalone HTML report ...
set "RPT="
for /f "delims=" %%F in ('dir /b /o-d "report\portfolio-*.html" 2^>nul') do (
  if not defined RPT set "RPT=report\%%F"
)
if defined RPT (
  echo   Report: %RPT%
  start "" "%RPT%"
) else (
  echo [WARN] Report file not found under report\.
)

echo [4/4] Optional local web server ...
if /i "%~1"=="--serve" (
  echo   Starting http://127.0.0.1:8767/portfolio
  start "BYD-Monitor-Server" /min cmd /c "set HOST=127.0.0.1&& set PORT=8767&& python -u byd_web_server.py"
  timeout /t 5 >nul
  start "" "http://127.0.0.1:8767/portfolio"
) else (
  echo   Skipped. Run  %~nx0 --serve  to also start the web server.
)

echo.
echo ----------------------------------------------------------
echo  Done. To deploy / update the Linux server:
echo    1) git add -A ^&^& git commit -m "update" ^&^& git push
echo    2) On the server:  sudo bash deploy/onekey.sh
echo ----------------------------------------------------------
echo  Disclaimer: research only, NOT investment advice.
echo.
pause
