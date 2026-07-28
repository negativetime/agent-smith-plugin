#!/usr/bin/env python3
"""Local audio transcription for agent-smith (Apple Silicon, via mlx-whisper).

    python3 transcribe.py recording.wav                 # transcript to stdout
    python3 transcribe.py memo.m4a --model small        # faster, less accurate
    python3 transcribe.py call.mp3 --timestamps         # [mm:ss] segment lines

Free, offline, private — audio never leaves the machine. First run downloads
the model (~1.6 GB for the default large-v3-turbo). Requires ffmpeg on PATH
for non-wav formats. Every run is logged to the usage ledger.
"""
import argparse
import json
import os
import sys
import time

MODELS = {
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "small": "mlx-community/whisper-small-mlx",
    "large": "mlx-community/whisper-large-v3-mlx",
}


def _ledger(rec):
    """Append one usage record to the skill's data/usage.jsonl. Never raises."""
    try:
        import datetime
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), **rec}
        path = os.environ.get("SMITH_LEDGER") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "usage.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", help="audio file (wav/mp3/m4a/aiff/...)")
    ap.add_argument("--model", default="turbo",
                    help="turbo (default) | small | large | any mlx-community repo")
    ap.add_argument("--language", default=None, help="e.g. en (default: autodetect)")
    ap.add_argument("--timestamps", action="store_true",
                    help="print [mm:ss] per segment instead of plain text")
    ap.add_argument("--tag", default="audio-transcribe", metavar="TASKSHAPE",
                    help="task-shape label for the ledger (default: audio-transcribe).")
    args = ap.parse_args()

    if not os.path.isfile(args.audio):
        print(f"ERROR: no such file: {args.audio}", file=sys.stderr)
        sys.exit(2)
    model = MODELS.get(args.model, args.model)
    t0 = time.time()
    try:
        import mlx_whisper
        opts = {"path_or_hf_repo": model}
        if args.language:
            opts["language"] = args.language
        result = mlx_whisper.transcribe(args.audio, **opts)
    except Exception as exc:  # noqa: BLE001 — single clear failure path for callers
        print(f"ERROR: transcription failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.timestamps:
        for seg in result.get("segments", []):
            m, s = divmod(int(seg["start"]), 60)
            print(f"[{m:02d}:{s:02d}] {seg['text'].strip()}")
    else:
        print(result.get("text", "").strip())
    sys.stdout.flush()

    dur = None
    segs = result.get("segments") or []
    if segs:
        dur = round(segs[-1].get("end", 0), 1)
    print(f"\n--- transcribe meta ---\nmodel: {model}  audio_s: {dur}  "
          f"wall_s: {round(time.time() - t0, 1)}", file=sys.stderr)
    _ledger({"script": "transcribe", "backend": "mlx", "model": model, "tag": args.tag,
             "audio": os.path.basename(args.audio), "audio_seconds": dur,
             "out_chars": len(result.get("text", "")),
             "seconds": round(time.time() - t0, 1), "status": "ok"})


if __name__ == "__main__":
    main()
