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
# Images are rendered in worker threads (asyncio.to_thread). glibc gives each
# thread its own malloc arena by default and rarely hands that memory back, so
# RSS creeps up; two arenas keep it flat at no visible cost for this workload.
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
export PYTHONIOENCODING=utf-8
exec .venv/bin/python main.py
