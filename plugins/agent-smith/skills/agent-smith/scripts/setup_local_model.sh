#!/usr/bin/env bash
#
# agent-smith — disk-aware local coding-model installer (Ollama backend).
#
# OPTIONAL. You only need this if you want the local/offline `--backend ollama`
# path. The default cloud backend (Gemini) needs no local model at all.
#
# It detects how much disk you have free and offers a model tier sized to it —
# more space lets you run a bigger, better coder. Picks are from agent-smith's
# eval harness (gpt-oss:20b holds the code crown: double perfect sweep, trusted
# for agentic app builds; qwen3-coder:30b = speed pick; gemma4:26b = vision+design).
#
# Usage:  bash setup_local_model.sh
#
set -euo pipefail

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama isn't installed. Get it from https://ollama.com, then re-run this." >&2
  exit 1
fi

# Free space on the volume Ollama stores models on. `df -k` is portable across
# macOS and Linux (avail KB in column 4); convert to whole GB.
store="${OLLAMA_MODELS:-$HOME/.ollama}"
[ -d "$store" ] || store="$HOME"
free_kb=$(df -k "$store" 2>/dev/null | awk 'NR==2 {print $4}')
free_gb=$(( ${free_kb:-0} / 1024 / 1024 ))

echo
echo "agent-smith — local coding model setup"
echo "Free disk available: ${free_gb} GB"
echo

# Tier table: key | model tag | approx GB on disk | one-line note
# (keep tag/size pairs in sync with the menu below)
# No Gemini key? Local models are a complete, no-account path — say so up front.
if [ -z "${GEMINI_API_KEY:-}${GOOGLE_API_KEY:-}" ]; then
  echo "No GEMINI_API_KEY detected — that's fine. A local model below runs agent-smith with"
  echo "NO account and NO cloud. (Or get a free key at https://aistudio.google.com/apikey to"
  echo "also enable the cloud Gemini backend, which is faster and stronger.)"
  echo
fi

recommend=""
if   [ "$free_gb" -ge 22 ]; then recommend="1 for code (gpt-oss:20b), 2 for speed, or 4 for vision (gemma4:26b)"
elif [ "$free_gb" -ge 16 ]; then recommend="1 for code (gpt-oss:20b)"
elif [ "$free_gb" -ge 12 ]; then recommend="3 for code (qwen2.5-coder:14b)"
elif [ "$free_gb" -ge 10 ]; then recommend="5 for general (gemma3:12b)"
else                             recommend="6 (llama3.2:3b) — low on space"
fi

echo "Best fit for your free space: ${recommend}"
echo
echo "Choose a local model (bigger = better, needs more space):"
echo "  -- for CODE (best local coders, from agent-smith's bake-off) --"
echo "  1) gpt-oss:20b         ~13 GB  RECOMMENDED — best measured local coder (trusted for"
echo "                                 code, extraction, repo edits, AND agentic app builds)"
echo "  2) qwen3-coder:30b     ~18 GB  fastest drafts (2-8s one-shots) — the speed pick"
echo "  3) qwen2.5-coder:14b   ~9 GB   lighter code fallback"
echo
echo "  4) gemma4:26b          ~17 GB  vision (reads images!) + design + general quality"
echo "  5) gemma3:12b          ~8 GB   small general model (older gen, low disk)"
echo "  6) llama3.2:3b         ~2 GB   tiny floor — light text only, not for agents"
echo "  7) skip"
echo
printf "Enter 1-7: "
read -r choice

case "$choice" in
  1) model="gpt-oss:20b";       need=16 ;;
  2) model="qwen3-coder:30b";   need=22 ;;
  3) model="qwen2.5-coder:14b"; need=12 ;;
  4) model="gemma4:26b";        need=21 ;;
  5) model="gemma3:12b";        need=11 ;;
  6) model="llama3.2:3b";       need=4  ;;
  7|"") echo "Skipped. Set GEMINI_API_KEY for the cloud backend, or re-run to pick a local model."; exit 0 ;;
  *) echo "Unrecognized choice '$choice'. Re-run and pick 1-7." >&2; exit 1 ;;
esac

# Guard against pulling something that won't fit (need = model size + headroom).
if [ "$free_gb" -lt "$need" ]; then
  echo "Heads up: ${model} needs ~${need} GB free (incl. headroom) but you have ${free_gb} GB." >&2
  printf "Pull anyway? [y/N]: "
  read -r ok
  case "$ok" in y|Y|yes|Yes) ;; *) echo "Aborted."; exit 1 ;; esac
fi

echo
echo "Pulling ${model} (this can take a few minutes)..."
ollama pull "$model"

echo
echo "Done. ${model} is installed."
if [ "$model" != "qwen3-coder:30b" ]; then
  echo "agent-smith's Ollama default is qwen3-coder:30b. To make ${model} the default instead,"
  echo "set:  export OLLAMA_MODEL=${model}   (add it to your shell profile to persist)."
  echo "Or pass it explicitly:  --backend ollama --model ${model}"
fi
