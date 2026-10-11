#!/usr/bin/env bash
# Starts the copilot UI in the background.
#
# Used as the Codespaces postStartCommand, so it must be idempotent: Codespaces
# re-runs it every time the machine starts, and a second bind on the same port
# would kill the first attempt with an OSError.
#
# VENDOR_RISK_TRANSPORT=inprocess runs the same mock_api.app code inside this
# process instead of on port 8001, so the whole app is one service. The outage
# simulation is a DataStore overlay flag, not a killed socket, so it is
# unaffected by this choice.
set -u

PORT=8501

if curl -sf "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
  echo "Copilot already running on :${PORT}"
  exit 0
fi

export VENDOR_RISK_TRANSPORT="${VENDOR_RISK_TRANSPORT:-inprocess}"

nohup python -m uvicorn app.api:app --host 0.0.0.0 --port "${PORT}" \
  > /tmp/copilot-ui.log 2>&1 &
echo "Started copilot UI (pid $!) — logs at /tmp/copilot-ui.log"
