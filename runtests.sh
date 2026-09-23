#!/bin/sh
# Run the fund-vista backend test suite inside the fundvista-dev container.
# Usage: runtests.sh [pytest-args...]
set -eu
cd /opt/fund-vista
docker run --rm -v /opt/fund-vista:/srv/fund-vista \
  -e PYTHONPATH=/srv/fund-vista \
  -e PENPLOT_DATA_DIR=/tmp/testdata \
  -e REDIS_CLOUD_URL=redis://127.0.0.1:1/0 \
  -w /srv/fund-vista \
  fundvista-dev python -m pytest backend/tests/ -q "$@" 2>&1