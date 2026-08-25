@echo off
rem スタートアップ登録用。ブラウザは自動で開かない（毎回タブが開くのを避けるため）。
rem 見たいときに http://127.0.0.1:8787 をブックマークから開く。
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python dashboard.py
