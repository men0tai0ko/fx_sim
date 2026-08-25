@echo off
rem リアルタイム仮想運用を起動する。ウィンドウを閉じると停止し、次回はその続きから再開する。
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python live_trade.py --interval 300
pause
