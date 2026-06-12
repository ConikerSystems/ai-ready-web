# AI Ready Mobile (web) — Claude Session Protocol

Public repo — the browser/PWA "Mobile" version of AI Ready for iPhone/iPad,
served on GitHub Pages (static, no server, no build step). User-facing name:
**AI Ready Mobile** (never "Lite" in UI copy).

- **This repo is PUBLIC.** Code only — NEVER commit documents, test PDFs, or any
  personal data (.gitignore blocks *.pdf etc.; keep it that way).

## Sync rule (Joe's standing requirement)
Mobile must stay **functionally in sync with the primary app** except the agreed
exclusions. Everything under `py/converters/` and `py/masking/` is a **VERBATIM
copy from the private AI_Ready repo** — fix there first, then re-copy here and
redeploy. `py/mobile.py` is the only web-specific Python (it also embeds
`_term_to_regex`/`_apply_custom_variables` copied from ai_ready app.py — keep
those in sync too). When a converter or masker changes in AI_Ready, the same
session should re-copy + push here.

**Agreed mobile exclusions (do NOT add):** Excel/CSV, scanned-doc OCR, audio +
Voice Memos, folder ingest, legacy .doc/.ppt, Large/Custom condense.
**Caps:** 20 files / 25 MB per file / 100 MB per batch.

## UX rule (Joe's standing requirement)
**No technical language in user-facing UI.** No "Python", "Pyodide", "WASM",
"Markdown", byte counts, logs, or previews. Apple-grade simplicity. Dev
telemetry lives ONLY behind `?debug=1` (debug card + console).

## Deploy
- GitHub Pages serves `main` root. `.nojekyll` is REQUIRED (Jekyll drops
  `_*` files like `__init__.py`) — never delete it.
- Every deploy: bump `static/js/version.js` AND the `CACHE` name in `sw.js`
  (single-source versioning + cache invalidation), then **verify the LIVE site
  in a real browser** — local testing alone has missed deploy-layer bugs.
- Pyodide is PINNED (v314.0.0 CDN). Test on a real iPhone/iPad before bumping.
- Local preview: `python3 -m http.server 8901 --directory .` (or the
  `ai-ready-web-poc` launch config from the ai_ready session).
