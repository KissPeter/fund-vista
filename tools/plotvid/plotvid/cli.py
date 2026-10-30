"""Command line: ``python -m plotvid detect config.yaml [--preview]``.

Later steps add ``plan``, ``render`` and ``all``.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from plotvid import audio, config, debug, media, probe


def _timecode(s: float) -> str:
    m, sec = divmod(s, 60)
    return f"{int(m):02d}:{sec:06.3f}"


def run_detect(cfg: config.Config, preview: bool) -> int:
    root = cfg.out_dir / "detect"
    root.mkdir(parents=True, exist_ok=True)
    sources = probe.order_sources(cfg.sources)
    overview = []
    for order, info in enumerate(sources):
        out = root / info.path.stem
        out.mkdir(exist_ok=True)
        if not info.has_audio:
            print(f"[{order}] {info.path.name}: no audio stream, skipped", file=sys.stderr)
            overview.append({"source": str(info.path), "error": "no audio"})
            continue

        x = media.extract_audio(info.path, cfg.detect.sample_rate)
        det = audio.detect(x, cfg.detect)

        with (out / "events.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["index", "time_s", "timecode", "level_db", "prominence_db", "gap_s"])
            prev = None
            for i, ev in enumerate(det.events):
                gap = "" if prev is None else f"{ev.time_s - prev:.3f}"
                w.writerow([i, f"{ev.time_s:.3f}", _timecode(ev.time_s), f"{ev.level_db:.1f}",
                            f"{ev.prominence_db:.1f}", gap])
                prev = ev.time_s
        debug.envelope_png(det, out / "envelope.png", info.path.name)
        if preview:
            wav = out / "beeps.wav"
            debug.beep_track(x, cfg.detect.sample_rate, det, wav)
            media.mux_preview(info.path, wav, out / "preview.mp4")
            wav.unlink()

        summary = {
            "source": str(info.path),
            "order": order,
            "created": info.created.isoformat(),
            "created_from": info.created_from,
            "duration_s": info.duration_s,
            "display_size": info.display_size,
            "rotation": info.rotation,
            "fps": info.fps,
            "detect": asdict(cfg.detect),
            "events": len(det.events),
            "first_event_s": det.events[0].time_s if det.events else None,
            "last_event_s": det.events[-1].time_s if det.events else None,
        }
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
        overview.append(summary)
        print(f"[{order}] {info.path.name}: {len(det.events)} pen events -> {out}")
    (root / "overview.json").write_text(json.dumps(overview, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="plotvid", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect", help="detect pen events and write hand-check artefacts")
    d.add_argument("config", type=Path)
    d.add_argument("--preview", action="store_true",
                   help="also write preview.mp4 with a beep at each detected event")
    args = ap.parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"plotvid: {tool} not found on PATH", file=sys.stderr)
            return 2
    try:
        cfg = config.load(args.config)
    except (config.ConfigError, OSError) as exc:
        print(f"plotvid: config: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "detect":
        return run_detect(cfg, args.preview)
    return 2
