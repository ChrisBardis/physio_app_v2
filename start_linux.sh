#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  ./setup_linux.sh
fi
.venv/bin/python run.py
