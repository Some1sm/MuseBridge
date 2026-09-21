@echo off
title Muse AI Bridge Daemon
echo ======================================================
echo   MUSE AI BRIDGE FOR CLAUDE CODE
echo   Listening on http://127.0.0.1:8765
echo   Tokens: 1 Billion Free Token Balance
echo ======================================================
echo.
python "%~dp0muse_bridge.py" serve
pause
