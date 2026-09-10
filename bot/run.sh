#!/usr/bin/env bash
# Launch the PZ Tambayan bot from source (Linux). Creates a venv on first run.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
fi

export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
exec .venv/bin/python main.py
