@echo off
setlocal
set "ROOT=%~dp0"
pushd "%ROOT%"
py -3 -m maxwell_daemon.launcher --repo-root "%ROOT%" %*
if errorlevel 1 (
  echo.
  echo Maxwell-Daemon could not start. Review the message above, then run:
  echo   py -3 -m maxwell_daemon.launcher --repo-root "%ROOT%" --dry-run
  pause
  popd
  exit /b 1
)
popd
