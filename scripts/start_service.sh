#!/usr/bin/env bash
# Starts the copilot UI in the background.
#
# Used as the Codespaces postStartCommand, so it must be idempotent and
# self-healing: Codespaces re-runs it every time the machine starts, and a
# second bind on the same port would kill the first attempt with an OSError.
#
# The Codespaces image ships a PEP 668 managed Python with no pip on the
# default interpreter, so the dependencies live in .venv. Create it on demand
# if it is missing, so a rebuilt codespace comes back up on its own.
#
# VENDOR_RISK_TRANSPORT=inprocess runs the same mock_api.app code inside this
# process instead of on port 8001, so the whole app is one service. The outage
# simulation is a DataStore overlay flag, not a killed socket, so it is
# unaffected by this choice.
set -u

cd "$(dirname "$0")/.."
PORT=8501

if curl -sf "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
  echo "Copilot already running on :${PORT}"
  exit 0
fi

if [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
else
  echo "Creating .venv and installing dependencies..."
  python3 -m venv .venv || exit 1
  .venv/bin/pip install --quiet -r requirements.txt || exit 1
  PYTHON=.venv/bin/python
fi

export VENDOR_RISK_TRANSPORT="${VENDOR_RISK_TRANSPORT:-inprocess}"

nohup "$PYTHON" -m uvicorn app.api:app --host 0.0.0.0 --port "${PORT}" \
  > /tmp/copilot-ui.log 2>&1 &
echo "Started copilot UI (pid $!) — logs at /tmp/copilot-ui.log"
