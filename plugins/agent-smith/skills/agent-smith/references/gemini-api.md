# Gemini API reference (for agent-smith)

Read this only when the `scripts/gemini.py` flags don't cover what you need. Everything here
goes through the **Gemini Developer API** (`generativelanguage.googleapis.com`) authenticated
with `GEMINI_API_KEY` — not Vertex AI.

## Models this account can use

Confirmed live for `generateContent` (run `python3 scripts/gemini.py --list-models` for the
current list — it changes):

| Alias in helper | Resolves to | Use for |
|---|---|---|
| `flash` | `gemini-flash-latest` | bulk summarize/extract/classify, simple drafts (default) |
| `pro` | `gemini-pro-latest` | hard reasoning, nuanced synthesis, tricky code |
| `flash-lite` | `gemini-flash-lite-latest` | highest-volume, lowest-stakes work |

Also available by full name (pass directly to `--model`): `gemini-2.5-flash`, `gemini-2.5-pro`,
`gemini-3.1-pro-preview`, `gemini-3.5-flash`, image models (`gemini-3-pro-image`,
`nano-banana-pro-preview`), TTS, and `deep-research-*` models. The `*-latest` aliases are the
safe default — they track Google's current recommended build without you hard-coding a version.

## Request shape

`POST https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}`

```json
{
  "contents": [{"role": "user", "parts": [{"text": "..."}, {"inlineData": {"mimeType": "...", "data": "<base64>"}}]}],
  "systemInstruction": {"parts": [{"text": "role / constraints"}]},
  "tools": [{"googleSearch": {}}],
  "generationConfig": {
    "temperature": 0.2,
    "maxOutputTokens": 4096,
    "responseMimeType": "application/json",
    "responseSchema": { /* JSON Schema */ },
    "thinkingConfig": {"thinkingBudget": 0}
  }
}
```

Notes:
- **Grounding tool** is `{"googleSearch": {}}` on 2.x/3.x models (the old 1.5 name
  `googleSearchRetrieval` is obsolete). Sources come back in
  `candidates[0].groundingMetadata.groundingChunks[].web.{uri,title}`. The helper prints them
  to stderr. The URIs are Google redirect links — fine to cite, they resolve to the real page.
- **Structured output**: set `responseMimeType: "application/json"`; optionally add
  `responseSchema` (a JSON Schema object) to constrain the shape. Don't combine with
  `googleSearch` — schema-constrained output and grounding don't mix well.
- **Thinking**: 2.5/3.x models "think" before answering (counted as `thoughtsTokenCount`).
  Set `thinkingConfig.thinkingBudget: 0` to disable on Flash for faster/cheaper bulk runs;
  raise it for harder reasoning. Pro may ignore a 0 budget.
- **`countTokens`** endpoint (`:countTokens`) estimates input size before a big call if you
  need to budget.

## Files: inline vs Files API

The helper picks automatically — inline base64 for files ≤ 15 MB, resumable Files API upload
above that. Manual notes if you ever need them:

- **Inline**: `{"inlineData": {"mimeType": "...", "data": "<base64>"}}`. Whole request must stay
  under ~20 MB.
- **Files API** (large/reused files): resumable upload to
  `POST /upload/v1beta/files` → returns a `file.uri` → reference as
  `{"fileData": {"mimeType": "...", "fileUri": "<uri>"}}`. Uploaded files persist ~48h and can
  be reused across calls without re-uploading. Supports PDF, images, audio, video, text.
- A single PDF can be up to ~1000 pages / 50 MB via the Files API. For bigger corpora, split or
  summarize in chunks and let Gemini (or you) merge.

## curl fallback

If Python is unavailable, a minimal grounded call.

macOS / Linux (bash):

```bash
curl -s "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key=$GEMINI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"contents":[{"parts":[{"text":"YOUR PROMPT"}]}],"tools":[{"googleSearch":{}}]}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['candidates'][0]['content']['parts'][-1]['text'])"
```

Windows (PowerShell) — note `curl` is an alias for `Invoke-WebRequest`, so call `curl.exe`, and
the env var is `$env:GEMINI_API_KEY`:

```powershell
curl.exe -s "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key=$env:GEMINI_API_KEY" `
  -H "Content-Type: application/json" `
  -d '{\"contents\":[{\"parts\":[{\"text\":\"YOUR PROMPT\"}]}],\"tools\":[{\"googleSearch\":{}}]}'
```

## Rate limits / quota

Free-tier keys have per-minute and per-day request caps that differ by model. `flash` and
`pro` draw from separate buckets, so switching tiers can dodge a transient 429. The helper
retries 429/500/503 with exponential backoff automatically. If you're doing a large batch,
prefer one big call over many small ones, and consider `gemini-flash-lite-latest`.

## Optional: the official Gemini CLI

Not installed here, and not needed (the API key path works with zero setup). If the user ever
wants the more generous *Google-account* free tier (Gemini Code Assist) instead of the API
key's quota, install with `npm i -g @google/gemini-cli` and run `gemini` to OAuth-login. The
helper script and this skill don't depend on it.
