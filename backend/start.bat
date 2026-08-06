@echo off
setlocal

rem This PC's IP on the sensor LAN. It decides which adapter mDNS discovery and
rem the sensor UDP commands bind to, so it must be the address on the sensor's
rem subnet — auto-detection picks the Wi-Fi adapter on this machine and then no
rem sensor is ever found. Keep it in step with NETWORK_IF in the Qt app's
rem config.py.
set NETWORK_IF=192.168.1.2

set WS_FPS=30

rem Bind the web server to every adapter: hard-coding one address means the app
rem refuses to start at all whenever the machine's IP changes.
"%~dp0~\venv-browser-viewer\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8000
