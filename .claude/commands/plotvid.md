---
description: Turn a folder of pen-plotter videos and photos into 9:16 clips (plotvid)
argument-hint: <source folder, e.g. /Users/me/pen-pixel/raw/lhbp>
allowed-tools: Bash(tools/plotvid/plotvid-folder:*), Bash(ls:*), Bash(ffprobe:*)
---

Run plotvid on the source folder `$ARGUMENTS`.

1. If no folder was given, ask for one. Do not guess.
2. Check the folder exists and holds at least one video (.mov, .mp4, .m4v) or photo.
3. Run `tools/plotvid/plotvid-folder "$ARGUMENTS"` from the repository root. It takes minutes, so run it in the background and wait for it to finish.
4. The clips go to the same path with `raw` replaced by `processed` (`.../raw/lhbp` -> `.../processed/lhbp/lhbp_NN.mp4`). Earlier clips of that name are replaced. Do not move or delete anything else.
5. Report: how many clips, the length of each, the output folder, and anything the log warned about (for example a file with no pen events). If the run failed, quote the `plotvid.folder:` line.

Do not change settings or code for this. The rules are in `tools/plotvid/README.md`; the defaults are in `tools/plotvid/folder.yaml`.
