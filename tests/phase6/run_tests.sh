#!/usr/bin/env bash
#
# Phase 6 integration tests — API server, ST sync, edit history, cross-phase pipelines.
#
# Uses an isolated home (symlinked holmes canon/worlds) so runs never touch a
# live project or mutate the holmes fixture workspace permanently.
#
# Usage:  bash tests/phase6/run_tests.sh
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$HERE/../.." && pwd)"
cd "$PROJECT_ROOT"

ISOLATED="$HERE/isolated"
mkdir -p "$ISOLATED/workspace"

export STORY_EDITOR_HOME="$(cd "$ISOLATED" && pwd)"
export STORY_EDITOR_LOG="$STORY_EDITOR_HOME/workspace/phase6.jsonl"
# A scratch project registry: the harness never reads or writes the real one.
export STORY_EDITOR_REGISTRY="$STORY_EDITOR_HOME/workspace/registry/projects.json"
export STORY_EDITOR_PROJECTS_DIR="$STORY_EDITOR_HOME/workspace/projects"
for d in canon worlds; do
  target="$HERE/../holmes/$d"
  link="$ISOLATED/$d"
  if [[ -L "$link" ]] && [[ ! -e "$link" ]]; then
    rm "$link"
  fi
  if [[ ! -e "$link" ]]; then
    ln -s "$target" "$link"
  fi
done

export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"

# The semantic-index probes need the embedder, which lives in the project's conda
# env rather than whatever python3 the shell happens to offer. Prefer it, and say
# so when falling back, because "no module named sentence_transformers" reported
# as two test failures wasted an afternoon once already.
PY="${STORY_EDITOR_PYTHON:-}"
if [[ -z "$PY" ]]; then
  for candidate in \
    "$(command -v python3 || true)"; do
    if [[ -x "$candidate" ]] && "$candidate" -c "import sentence_transformers" 2>/dev/null; then
      PY="$candidate"
      break
    fi
  done
fi
if [[ -z "$PY" ]]; then
  PY="$(command -v python3)"
  echo "note: no interpreter with sentence_transformers found — the two"
  echo "      semantic-index probes will fail. Set STORY_EDITOR_PYTHON to the"
  echo "      env that has it."
fi
echo " python=$PY"

API_PORT="${STORY_EDITOR_API_PORT:-18766}"
API_BASE="http://127.0.0.1:${API_PORT}"

echo "=============================================================="
echo " Phase 6 tests — home=$STORY_EDITOR_HOME"
echo " API=$API_BASE"
echo "=============================================================="

# Start API server in background.
"$PY" -m story_editor serve --host 127.0.0.1 --port "$API_PORT" &
SERVER_PID=$!
cleanup() { kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT

# Wait until /health responds.
for _ in $(seq 1 30); do
  if curl -sf "$API_BASE/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
if ! curl -sf "$API_BASE/health" >/dev/null 2>&1; then
  echo "FAIL: API server did not start on $API_BASE"
  exit 1
fi

"$PY" "$HERE/test_phase6.py" --api-base "$API_BASE"
exit $?
