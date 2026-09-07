@echo off
cd /d "%~dp0"
echo Starting Technocore Keepalive Service...
python keepalive.py --interval-hours 4
pause