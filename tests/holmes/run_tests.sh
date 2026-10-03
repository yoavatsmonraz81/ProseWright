#!/usr/bin/env bash
#
# Holmes fixture integration test runner.
#
# Exercises the whole engine against an isolated, non-SillyTavern story (the
# "A Scandal in Bohemia" excerpt) so a full run can never touch a live
# project's files. Isolation is via STORY_EDITOR_HOME (see config.py).
#
# Two phases:
#   PHASE A — non-LLM checks (loader, importer, structure segmentation, doctor,
#             canon data, keyword index). Always run.
#   PHASE B — LLM-dependent checks (scene cards, beat derivation, restyle,
#             canon check/test, proofread, sweep). Run only if the model server
#             at $STORY_EDITOR_MODEL_URL is reachable.
#
# Usage:  bash tests/holmes/run_tests.sh
#
set -uo pipefail

# --- locate project root (two levels up from this script) -------------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$HERE/../.." && pwd)"
cd "$PROJECT_ROOT"

export STORY_EDITOR_HOME="$HERE"
export STORY_EDITOR_LOG="$HERE/workspace/scandal.jsonl"

# Use the locally-cached embedding model; don't reach out to HuggingFace. The
# BAAI/bge-small model is cached after the first index build.
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"

CLI="python3 -m story_editor.cli"
MODEL_URL="${STORY_EDITOR_MODEL_URL:-http://127.0.0.1:5000/v1}"

PASS=0; FAIL=0; SKIP=0
declare -a RESULTS

run() {  # run "label" cmd...
  local label="$1"; shift
  echo
  echo "──────────────────────────────────────────────────────────────"
  echo ">> $label"
  echo "   \$ $*"
  if "$@"; then
    echo "   PASS: $label"
    PASS=$((PASS+1)); RESULTS+=("PASS  $label")
  else
    echo "   FAIL: $label (exit $?)"
    FAIL=$((FAIL+1)); RESULTS+=("FAIL  $label")
  fi
}

skip() { echo "   SKIP: $1"; SKIP=$((SKIP+1)); RESULTS+=("SKIP  $1"); }

echo "=============================================================="
echo " Holmes fixture test  —  STORY_EDITOR_HOME=$STORY_EDITOR_HOME"
echo "=============================================================="

# ============================ PHASE A (no LLM) ==============================
echo; echo "### PHASE A — non-LLM checks"

run "import: .story -> .jsonl"          $CLI import "$HERE/source/scandal_in_bohemia.story" -o "$STORY_EDITOR_LOG"
run "list: log loads"                   $CLI list
run "structure doctor: integrity"       $CLI structure doctor
run "structure scenes: segmentation"    $CLI structure scenes
run "structure episodes: grouping"      $CLI structure episodes
run "canon list: bibles load"           $CLI canon list
run "canon show: Holmes"                $CLI canon show Holmes
run "canon show: King (alias)"          $CLI canon show "Count von Kramm"

# index build needs the local embedder (BAAI/bge-small). Treat as its own step.
run "index build"                       $CLI index build --rebuild
run "index search (keyword)"            $CLI index search "photograph" --mode keyword --limit 5

# ========================== PHASE B (LLM needed) ============================
echo; echo "### PHASE B — LLM-dependent checks"

if curl -s --max-time 4 "$MODEL_URL/models" >/dev/null 2>&1; then
  echo "   model server reachable at $MODEL_URL"

  run "structure cards (LLM)"           $CLI structure cards
  run "structure derive (LLM)"          $CLI structure derive
  run "structure commit"                $CLI structure commit

  # Transform + gates. Propose a restyle, audit it, proof-read it, then discard
  # so the run is idempotent (no committed change left behind).
  run "restyle propose (LLM)"           $CLI restyle --speaker Holmes --from 2 --to 2 --note "make the deduction even more clipped and precise"
  run "canon check (LLM)"               $CLI canon check
  run "proofread (LLM)"                 $CLI proofread
  run "edits show"                      $CLI edits show
  run "edits discard"                   $CLI edits discard

  # Bible proof: violating vs compliant, per character.
  run "canon test Holmes (LLM)"         $CLI canon test Holmes
  run "canon test King (LLM)"           $CLI canon test King

  # Inject dry-run (no model call) + a real inject + discard.
  run "inject dry-run"                  $CLI inject --after 22 --speaker Watson --note "a closing line" --dry-run
else
  echo "   model server NOT reachable at $MODEL_URL — skipping LLM checks"
  skip "structure cards (needs model server)"
  skip "structure derive (needs model server)"
  skip "structure commit (needs model server)"
  skip "restyle propose (needs model server)"
  skip "canon check (needs model server)"
  skip "proofread (needs model server)"
  skip "edits show/discard (needs model server)"
  skip "canon test Holmes (needs model server)"
  skip "canon test King (needs model server)"
  skip "inject dry-run (needs model server)"
fi

# ========================== PHASE D (Phase 6 API) ===========================
echo; echo "### PHASE D — Phase 6 API + cross-phase pipelines"

run "phase6: API / sync / history"  bash "$HERE/../phase6/run_tests.sh"

# ================================ SUMMARY ===================================
echo
echo "=============================================================="
echo " SUMMARY    PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP"
echo "=============================================================="
for r in "${RESULTS[@]}"; do echo "  $r"; done

[ "$FAIL" -eq 0 ]
