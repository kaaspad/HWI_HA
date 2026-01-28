#!/bin/bash
# Run protocol-layer tests WITHOUT Home Assistant dependencies
#
# Usage: ./run_tests.sh [pytest-options]
#
# This script temporarily renames __init__.py to avoid loading
# the Home Assistant integration during test discovery.

set -e
cd "$(dirname "$0")"

# Temporarily hide the HA integration __init__.py
if [ -f __init__.py ]; then
    mv __init__.py __init__.py.bak
    trap "mv __init__.py.bak __init__.py 2>/dev/null || true" EXIT
fi

# Run pytest
python -m pytest tests/ "$@"
