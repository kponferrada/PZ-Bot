@echo off
rem Launch the PZ Tambayan bot from source (Windows). Creates a venv on first run.
cd /d "%~dp0"

if not exist .venv (
    py -m venv .venv
    .venv\Scripts\pip install --upgrade pip
    .venv\Scripts\pip install -r requirements.txt
)

set PYTHONUNBUFFERED=1
.venv\Scripts\python main.py
