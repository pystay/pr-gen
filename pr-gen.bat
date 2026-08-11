@echo off
rem pr-gen 启动脚本（Windows）：在任意目录下运行，自动定位包目录
setlocal
set "DIR=%~dp0"
if defined PYTHONPATH (
  set "PYTHONPATH=%DIR%;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%DIR%"
)
python -m pr_gen %*
exit /b %ERRORLEVEL%
