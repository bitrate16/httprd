@echo off

@REM Start server
set VIEW_PASSWORD=""
set PORT=7417

python httprd.py --port=%PORT% --view_password=%VIEW_PASSWORD%
pause
