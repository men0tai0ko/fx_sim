@echo off
rem 運用状況をブラウザで確認する。表示専用で、売買には一切関与しない。
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python dashboard.py --open
pause
