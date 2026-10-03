#!/bin/sh
# Run the fund-vista backend test suite inside the fundvista-dev container.
# Usage: runtests.sh [pytest-args...]
#
# fundvista-dev is the repo Dockerfile plus the [test] group. pip cannot
# install dependency-groups, so they ride an explicit second layer.
# Rebuild it after any backend/pyproject.toml change — a stale image
# silently predates new deps (seen 2026-10-03: a 9-day-old image without
# the fonttools pin turned 40 font-path tests red on an otherwise green
# main):
#   docker build -t fundvista-dev . \
#     && printf 'FROM fundvista-dev\nRUN pip install pytest httpx\n' \
#       | docker build -t fundvista-dev -
set -eu
cd /opt/fund-vista
docker run --rm -v /opt/fund-vista:/srv/fund-vista \
  -e PYTHONPATH=/srv/fund-vista \
  -e PENPLOT_DATA_DIR=/tmp/testdata \
  -e REDIS_CLOUD_URL=redis://127.0.0.1:1/0 \
  -w /srv/fund-vista \
  fundvista-dev python -m pytest backend/tests/ -q "$@" 2>&1