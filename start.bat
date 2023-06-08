@echo off

@REM Start server
set PASSWORD=""
set VIEW_PASSWORD=""
set PORT=7417

python httprd.py --port=%PORT% --password=%PASSWORD% --view_password=%VIEW_PASSWORD%
pause
