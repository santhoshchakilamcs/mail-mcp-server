#!/usr/bin/env bash
# mail-mcp-server helper: setup, build the demo mailbox, serve, eval, test.
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  echo "usage: ./run.sh {setup|build|serve|serve-http|eval|verify|test|go-test}"
  exit 1
}

case "${1:-}" in
  setup)
    python3 -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install -r requirements.txt
    echo "venv ready: .venv/bin/python"
    ;;
  build)
    .venv/bin/python scripts/build_mailbox.py
    .venv/bin/python eval/generate_tasks.py
    ;;
  serve)
    # MCP server over stdio (the standard transport for MCP clients)
    .venv/bin/python -m src.server
    ;;
  serve-http)
    # MCP server over streamable HTTP on port 8000 (Docker/Kubernetes)
    .venv/bin/python -m src.server --http --port 8000
    ;;
  eval)
    .venv/bin/python eval/run_eval.py
    ;;
  verify)
    .venv/bin/python verify_results.py
    ;;
  test)
    .venv/bin/python -m pytest tests/ -q
    ;;
  go-test)
    export PATH="/usr/local/go/bin:$PATH"
    (cd go-client && go test ./...)
    ;;
  *)
    usage
    ;;
esac
