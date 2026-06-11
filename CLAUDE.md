# AI Ready Lite (Web) — Claude Session Protocol

Public repo — the browser/PWA "lite" version of AI Ready for iPhone/iPad,
served on GitHub Pages (SimpliPiano-style: static, no server, no build step).

- **This repo is PUBLIC.** Code only — NEVER commit documents, test PDFs, or any
  personal data (.gitignore blocks *.pdf etc.; keep it that way).
- The Python pipeline in `py/converters/` is **copied from the private
  `AI_Ready` repo** (source of truth for converter logic — fix bugs there first,
  then re-copy). Web-only shell code (HTML/JS) lives here.
- Follow `../WEB_APP_STANDARDS.md` (Mac sessions) for PWA patterns: service
  worker + cache versioning, in-app Update button, never-trap navigation,
  Coniker Systems footer/About.
- Lite scope: digital PDFs/DOCX/PPTX/XLSX only; NO OCR, audio, Voice Memos,
  folder ingest, legacy .doc/.ppt, or Custom/Large condense. Caps: 25 MB/file,
  20 files, 100 MB/batch.
- Pyodide is PINNED (v314.0.0 CDN). Test on a real iPhone/iPad after iOS
  updates before bumping anything.
- Local preview: `python3 -m http.server 8901 --directory .` (or the
  `ai-ready-web-poc` launch config from the ai_ready session).

Read `HANDOFF.md` (when present) for session continuity.
