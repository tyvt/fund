@echo off
cd /d "%~dp0"
python -m dividend_lowvol_rotation.report %*
