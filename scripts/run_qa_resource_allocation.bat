@echo off
REM Build testmon baseline (if needed), force a selective rerun, compute QA Resource Allocation.
setlocal
pushd "%~dp0\.."
set "PYTHONPATH=%CD%;%PYTHONPATH%"

if exist ".testmondata" del /f /q ".testmondata" >nul 2>&1

python -m pytest -o addopts= --testmon -q tests
if errorlevel 1 (
  echo Baseline pytest --testmon failed.
  popd
  exit /b 1
)

python testmon_metrics\compute_qa_resource_allocation.py --source src --tests tests
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
