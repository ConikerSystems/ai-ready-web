# AI Ready Lite — Web (Proof of Concept)

Browser version of [AI Ready](https://conikersystems.com): converts documents to
AI-ready Markdown **entirely on your device** — files are never uploaded anywhere.
Python runs in the browser via [Pyodide](https://pyodide.org) (WebAssembly), using
the same conversion pipeline as the desktop app.

**Status: proof of concept.** Single PDF → Markdown, with timing and memory
telemetry. The full lite app (masking, replacements, indexes, multi-file zip
output, PWA install) comes next if device testing holds up.

## Try it

Open the GitHub Pages URL on an iPhone, iPad, or any browser. First load downloads
~26 MB (Python runtime + PDF engine), cached afterward. Pick a PDF, tap Convert.

## Scope (lite vs desktop)

The lite version handles digital documents only, with size caps suited to phone
memory. Scanned-PDF OCR, audio transcription, folder ingest, legacy .doc/.ppt,
and large legal productions remain desktop-app features.

© Coniker Systems™
