#!/usr/bin/env bash
#
# agent-smith — disk-aware local coding-model installer (Ollama backend).
#
# OPTIONAL. You only need this if you want the local/offline `--backend ollama`
# path. The default cloud backend (Gemini) needs no local model at all.
#
# It detects how much disk you have free and offers a model tier sized to it —
# more space lets you run a bigger, better coder. Picks are from agent-smith's
# coding bake-off (qwen2.5-coder family won the local slot; 14B is the sweet spot).
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
if   [ "$free_gb" -ge 22 ]; then recommend="1 for code (qwen3-coder:30b) or 5 for general (gemma3:27b)"
elif [ "$free_gb" -ge 12 ]; then recommend="2 for code (qwen2.5-coder:14b) or 4 for general (gemma3:12b)"
elif [ "$free_gb" -ge 7  ]; then recommend="3 for code (qwen2.5-coder:7b) or 4 for general (gemma3:12b)"
else                             recommend="6 (llama3.2:3b) — low on space"
fi

echo "Best fit for your free space: ${recommend}"
echo
echo "Choose a local model (bigger = better, needs more space):"
echo "  -- for CODE (best local coders, from agent-smith's bake-off) --"
echo "  1) qwen3-coder:30b     ~18 GB  recommended — best local coder (30B MoE, 3B active, fast; benchmarked default)"
echo "  2) qwen2.5-coder:14b   ~9 GB   lighter, solid runner-up"
echo "  3) qwen2.5-coder:7b    ~5 GB   smallest & fastest"
echo "  -- for GENERAL text / no-Gemini-account use (Google's open Gemma) --"
echo "  4) gemma3:12b          ~8 GB   well-rounded general model"
echo "  5) gemma3:27b          ~17 GB  strongest general Gemma, if you have space"
echo "  6) llama3.2:3b         ~2 GB   tiny floor — light text only"
echo "  7) skip"
echo
printf "Enter 1-7: "
read -r choice

case "$choice" in
  1) model="qwen3-coder:30b";   need=22 ;;
  2) model="qwen2.5-coder:14b"; need=12 ;;
  3) model="qwen2.5-coder:7b";  need=8  ;;
  4) model="gemma3:12b";        need=11 ;;
  5) model="gemma3:27b";        need=21 ;;
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
