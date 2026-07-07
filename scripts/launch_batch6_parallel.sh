#!/usr/bin/env bash
# Launch the 11-model parametric filter on batch 6 (batch 6A + 6C + residuals).
# ~273 new candidates parsed from R3_WORKSHEET.md sections marked "batch 6".
# Same setup as batch2-5 for methodological consistency: mixed direct APIs
# for frontier + OpenRouter for open weights. Total ~$85-100, ~1h30-2h wall.
set -euo pipefail

cd "$(dirname "$0")/.."
source .venv/bin/activate

LOG_DIR="/tmp/pf_batch6"
mkdir -p "$LOG_DIR"

# 11 models: 5 frontier direct (Opus 4.7/4.8, Sonnet 4.6, GPT-5.4/5.5) +
# 6 open via OpenRouter (Mistral, Llama 4, Qwen 2.5, Gemma 3, Gemini 2.5, GLM 5.2).
# Gemini 2.5 Pro forced to runs=1 (reasoning mandatory, otherwise ~10x slower).
declare -a MODELS=(
  "claude-opus-4-7"
  "claude-opus-4-8"
  "claude-sonnet-4-6"
  "gpt-5.4"
  "gpt-5.5"
  "openrouter/mistralai/mistral-large-2407"
  "openrouter/meta-llama/llama-4-maverick"
  "openrouter/qwen/qwen-2.5-72b-instruct"
  "openrouter/google/gemma-3-27b-it"
  "openrouter/z-ai/glm-5.2"
)
GEMINI_MODEL="openrouter/google/gemini-2.5-pro"   # separate: runs=1

echo "Launching ${#MODELS[@]} + 1 (Gemini) parallel filter runs on batch 6..."
echo "Logs: $LOG_DIR/"
echo

PIDS=()
LABELS=()

for M in "${MODELS[@]}"; do
    SLUG=$(echo "$M" | sed 's|^openrouter/||; s|/|_|g')
    LOG="$LOG_DIR/${SLUG}.log"
    SUFFIX="_batch6_${SLUG}"
    echo "  [$SLUG] runs=4 -> $LOG"
    python -u scripts/parametric_filter.py \
        --model "$M" \
        --runs 4 \
        --only-new \
        --suffix "$SUFFIX" \
        > "$LOG" 2>&1 &
    PIDS+=($!)
    LABELS+=("$M")
done

# Gemini 2.5 Pro: runs=1 because reasoning is mandatory and ~10x slower
SLUG=$(echo "$GEMINI_MODEL" | sed 's|^openrouter/||; s|/|_|g')
LOG="$LOG_DIR/${SLUG}.log"
SUFFIX="_batch6_${SLUG}"
echo "  [$SLUG] runs=1 -> $LOG (reasoning mandatory, single draw)"
python -u scripts/parametric_filter.py \
    --model "$GEMINI_MODEL" \
    --runs 1 \
    --only-new \
    --suffix "$SUFFIX" \
    > "$LOG" 2>&1 &
PIDS+=($!)
LABELS+=("$GEMINI_MODEL")

echo
echo "All ${#PIDS[@]} processes launched. PIDs: ${PIDS[*]}"
echo "Watch progress:            for L in $LOG_DIR/*.log; do echo -n \"\$(basename \$L .log): \"; grep -cE '^\\[' \$L; done"
echo "Tail all:                  tail -f $LOG_DIR/*.log"
echo
echo "Waiting for all to finish (this shell will block)..."

FAILED=0
for i in "${!PIDS[@]}"; do
    if wait "${PIDS[$i]}"; then
        echo "  ✓ ${LABELS[$i]} finished"
    else
        echo "  ✗ ${LABELS[$i]} FAILED (see log)"
        FAILED=$((FAILED + 1))
    fi
done

echo
if [ $FAILED -eq 0 ]; then
    echo "All 11 runs completed. Merge with: python scripts/merge_batch25_reports.py"
    echo "(the merge script globs all _batch2-5_*.json AND _batch6_*.json patterns —"
    echo "adapt or add batch6-only merge as needed)"
else
    echo "$FAILED runs failed. Inspect their logs; re-run just those models."
fi
