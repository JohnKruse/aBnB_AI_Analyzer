#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$PROJECT_ROOT"

# ABNB_PYTHON may name an existing Python interpreter. The default environment
# stays local to this project and does not alter the system or Conda base.
PYTHON_BIN="${ABNB_PYTHON:-python3}"
if [ ! -d ".venv" ]; then
    echo "==> Creating virtual environment in .venv..."
    "$PYTHON_BIN" -m venv .venv
fi

# Check the complete app dependency set, not merely pandas. An environment with
# pandas but no Streamlit/map components must not be mistaken for a ready app.
if ! ./.venv/bin/python - <<'PY'
import importlib
import sys

modules = (
    'streamlit', 'streamlit_folium', 'st_aggrid', 'folium', 'pyairbnb',
    'pandas', 'numpy', 'yaml', 'requests', 'openai',
)
missing = []
for name in modules:
    try:
        importlib.import_module(name)
    except ImportError:
        missing.append(name)
if missing:
    print('Missing app dependencies: ' + ', '.join(missing), file=sys.stderr)
    sys.exit(1)
PY
then
    echo "==> Installing browser app dependencies into .venv..."
    ./.venv/bin/python -m pip install -r requirements-app.txt
fi

echo "==> Environment ready. Starting Unified Airbnb AI Analyzer..."
exec ./.venv/bin/python -m streamlit run review_app.py "$@"
