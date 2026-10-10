---
name: prepare-plotter-svg
description: Turn arbitrary raster images (JPG/PNG/scans, technical drawings, side views) into good-quality single-stroke pen-plotter SVGs with img2plot, verify them (visually + against ground truth), stamp ownership metadata and push them to the repo. Use when asked to convert, regenerate or QA drawings/images as plotter SVG, or to add them to the gallery.
---

# Prepare a good plotter SVG from any image

Tool: `python -m backend.penplot.img2plot` (same pipeline as `/penplot`; manual in
`docs/img2plot-cli.md`, design in `docs/F-005-technical-drawing.md`, metadata in
`docs/svg-metadata.md`). The sections below are the *workflow and judgement*; flags live in the manual.

## 0. Set up (once)

- Work in a **git worktree** of `origin/main`, never in a shared checkout another session may be switching:
  `git worktree add --detach "$SCRATCH/fv" origin/main && cd "$SCRATCH/fv/backend" && uv sync --group test`
  (run the CLI from the worktree root: `backend/.venv/bin/python -m backend.penplot.img2plot`).
- macOS needs `brew install tesseract` (Linux uses the bundled `tesserocr` wheel). Without OCR the run still
  works but numbers stay traced lines (`ocr_unavailable` warning).
- Keep scratch output (`out/`, `png/`, data dir) outside the repo. Point the OCR cache at scratch:
  `PENPLOT_DATA_DIR=$SCRATCH/data`.
- Fast Mac: lift the server guards so `--jobs N` really runs in parallel and OCR is not cut short:
  `PENPLOT_OCR_MAX_CONCURRENT=N PENPLOT_TRACE_MAX_CONCURRENT=N PENPLOT_OCR_BUDGET_S=600 PENPLOT_OCR_QUEUE_S=900`.

## 1. Triage the source

Only line art converts well (CAD/blueprint/dimension drawings, logos, side views). Photos and shaded art
do not become good centrelines; say so instead of shipping them. Look at the image first and note:
dimension numbers/labels, wheels/circles, a page frame, pale or thin lines, resolution (labels are
readable down to about 7 px tall; below that expect misses).

**Licence check**: converted images are derivatives. Only stamp "all rights reserved" / non-commercial on
images the owner may license; record the source (URL/site) in `--description`.

## 2. Convert ONE image first, and look at it

```
python -m backend.penplot.img2plot src.jpg -o out/one.svg
rsvg-convert -w 1600 -b white out/one.svg -o out/one.png   # then open the PNG next to the source
```

Defaults are the `/penplot` "Technical drawing" preset (centreline trace, 3x upscale, circles, OCR text,
page A4 landscape). Judge, in this order: missing/merged lines, wheels round, numbers drawn as clean
text (not squiggles), nothing wrong-but-confident (a wrong digit is worse than a traced squiggle),
frame removed if it should be (`--keep-frame` to keep, `page_frame_removed` warning confirms).
Read the warnings in the output: `ocr_low_confidence_words_kept_as_lines` (unsure labels stay lines),
`trace_upscale_clamped` (image too big for the memory cap), `ocr_time_budget_exceeded`.

## 3. Tune only with a reason

| Symptom | Try |
|---|---|
| pale/thin lines or seats missing | `--threshold 235` (raise), `--contrast 1.3` |
| noisy scan, speckle | `--threshold 180` (lower), `--prune-px` up |
| lines melted together (louvres, dense hatching) | keep `--upscale 3`, lower `--blur`; do not lower the mask level |
| numbers missing | check the source size first; raise `--contrast`; then see section 5 |
| wrong digits accepted | raise `--min-conf` (preset 30; 20 let wrong ones through) |
| page border should stay | `--keep-frame` |

Change one thing at a time; re-run the single image. Do not tune per image if a batch will follow:
the same flags must work for the whole set or the result is not reproducible on `/penplot`.

## 4. Batch

```
python -m backend.penplot.img2plot images/ --out-dir out/ --report out/report.json --jobs 8 -q \
  --creator "<owner>" --rights "© <year> <owner>. All rights reserved. <terms>" \
  --license all-rights-reserved --attribution-url <site> \
  --description "<what it is>, traced from <source>."
```

`--skip-existing` makes reruns resumable; exit code is non-zero if any image failed; the report has one
record per image (strokes, seconds, warnings). Expect roughly 5-15 s per drawing on a laptop core (the
single-line OCR re-read made a 77-image run ~15 min at `--jobs 8`).

**Metadata is mandatory for anything we publish**: every SVG gets `<title>` (file name by default),
`<desc>`, RDF creator/rights/licence. Never invent the owner or licence: ask. Clients cannot override it
server-side (`PENPLOT_SVG_*`); the CLI flags are for offline runs.

## 5. Verify quality (do not skip)

1. **Mechanical**: all outputs exist, `xmllint --noout` clean, `grep -L 'cc:Work' out/*.svg` empty, report
   has 0 failed, no stray `.jpg` copied into the gallery folder.
2. **Visual**: rasterise everything in parallel
   (`ls out/*.svg | xargs -P 8 -I{} sh -c 'rsvg-convert -w 1600 -b white {} -o png/$(basename {} .svg).png'`)
   and open a spread: the busiest, the faintest, the smallest-text, and any with many warnings.
3. **Numbers, objectively** (this is where quality is lost): hand-write the numbers of 3-4 varied drawings
   as ground truth, run the OCR engine on the source (`SheetEngine` + `detect_words`, see
   `backend/tests/test_penplot_ocr_text.py`) and score correct / wrong / missed. Accept a change only if
   correct goes up and wrong does not. Compare old vs new over the *whole* set (accepted-word count,
   images with none) to catch regressions in images you did not look at.
4. Report honestly: how many numbers are right, which are wrong or missing, and why (source too small,
   thin serif font, text touching a line). Never claim "all accurate".

Known limits: 7-px serif digits are near Tesseract's floor; letters are not read; partly hidden wheels
stay traced; `tessdata_best` was measured and is not better (3x slower, mixed).

## 6. Push

- Gallery SVGs live in the shop repo (`public/artwork/trains/` for trains), OCR/pipeline code in
  fund-vista. Different repos, different branches: one feature per branch
  (`feat/<name>`), linear history, **never force-push or amend pushed commits** (Lovable syncs this repo).
- Replace files in a shop worktree of `origin/main`, `git add` only the SVG folder, commit, push the
  branch, then open a PR (or give the PR link). Do not push to `main` unless the user says so in words.
- Code changes (OCR/preset) need tests first: `backend/.venv/bin/python -m pytest backend -q` must pass.
  If the fund-vista change should reach `/penplot`, the shop's submodule pointer bump is a separate PR.
- Commit message: what changed, the measured before/after (e.g. numbers correct 36 -> 43 on N drawings),
  and the licence/owner used.
