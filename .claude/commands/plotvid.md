---
description: Turn a folder of pen-plotter videos and photos into 9:16 clips, optionally one set per language (plotvid)
argument-hint: <source folder, e.g. /Users/me/pen-pixel/raw/lhbp>
allowed-tools: Bash(tools/plotvid/plotvid-folder:*), Bash(ls:*), Bash(ffprobe:*)
---

Run plotvid on the source folder `$ARGUMENTS`.

1. If no folder was given, ask for one. Do not guess.
2. Check the folder exists and holds at least one video (.mov, .mp4, .m4v) or photo.
3. Run `tools/plotvid/plotvid-folder "$ARGUMENTS"` from the repository root. It takes minutes, so run it in the background and wait for it to finish.
   For a `-hu` and `-en` set with the intro overlay, add `--intro hu=tools/plotvid/common/intro-hu.svg --intro en=tools/plotvid/common/intro-en.svg`. To re-render an earlier result unchanged, add `--plan <processed folder>/<name>.plan.json`.
4. The clips go to the same path with `raw` replaced by `processed` (`.../raw/lhbp` -> `.../processed/lhbp/lhbp_NN.mp4`, or `lhbp_NN-hu.mp4` / `-en.mp4` with `--intro`). Earlier clips of that name are replaced. Do not move or delete anything else.
5. Report: how many clips, the length of each, the output folder, and anything the log warned about (for example a file with no pen events). If the run failed, quote the `plotvid.folder:` line.

Do not change settings or code for this. The rules are in `tools/plotvid/README.md`; the defaults are in `tools/plotvid/folder.yaml`.
