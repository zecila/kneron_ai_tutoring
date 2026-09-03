#!/bin/sh

set -eu

python -c "from server import init_tts; init_tts(require_ready=True)"

exec gunicorn -w 4 -b 0.0.0.0:5000 --timeout 120 server:app
