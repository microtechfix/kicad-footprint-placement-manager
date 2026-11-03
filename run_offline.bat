@echo off
REM Offline preview of a board file (read-only). Usage: run_offline.bat path\to\board.kicad_pcb
python "%~dp0fpm_cli.py" %1 --ui
