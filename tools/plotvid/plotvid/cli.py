"""Command line: ``python -m plotvid {detect,plan,render,all} config.yaml``.

- ``detect``: pen events plus hand-check artefacts.
- ``plan``: runs detection and the ink check (both cached per source), then
  labels strokes, picks segments and assigns them to clips. It writes a
  manifest per clip and does no rendering.
- ``render``: renders the manifests ``plan`` wrote.
- ``all``: ``plan`` followed by ``render``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from plotvid import (
    assign,
    audio,
    config,
    debug,
    events,
    ink,
    manifest,
    media,
    probe,
    render,
)


def _timecode(s: float) -> str:
    m, sec = divmod(s, 60)
    return f"{int(m):02d}:{sec:06.3f}"


def _write_events(path: Path, events: list[audio.Event]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["index", "time_s", "timecode", "level_db", "prominence_db", "width_ms",
                    "gap_s"])
        prev = None
        for i, ev in enumerate(events):
            gap = "" if prev is None else f"{ev.time_s - prev:.3f}"
            w.writerow([i, f"{ev.time_s:.3f}", _timecode(ev.time_s), f"{ev.level_db:.1f}",
                        f"{ev.prominence_db:.1f}", f"{ev.width_s * 1000:.0f}", gap])
            prev = ev.time_s


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

        _write_events(out / "events.csv", det.events)
        _write_events(out / "dropped.csv", det.dropped)
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
            "dropped_by_width": len(det.dropped),
            "first_event_s": det.events[0].time_s if det.events else None,
            "last_event_s": det.events[-1].time_s if det.events else None,
        }
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
        overview.append(summary)
        print(f"[{order}] {info.path.name}: {len(det.events)} pen events, "
              f"{len(det.dropped)} dropped by width -> {out}")
    (root / "overview.json").write_text(json.dumps(overview, indent=2))
    return 0


CACHE_VERSION = 1


def _cache_path(cfg: config.Config, info: probe.SourceInfo) -> Path:
    st = info.path.stat()
    key = json.dumps([CACHE_VERSION, str(info.path), st.st_size, st.st_mtime_ns,
                      asdict(cfg.detect), asdict(cfg.ink)], sort_keys=True, default=str)
    digest = hashlib.sha1(key.encode()).hexdigest()[:12]
    return cfg.out_dir / "cache" / f"{info.path.stem}-{digest}.json"


def analyse_source(cfg: config.Config, info: probe.SourceInfo, want_clean: bool) -> dict:
    """Pen events, per-stroke new ink and (optionally) the clean final-frame time.

    Cached under out_dir/cache, keyed by path, size, mtime and the detect
    and ink configs, so re-planning with other segment or videos settings
    is instant."""
    path = _cache_path(cfg, info)
    data = json.loads(path.read_text()) if path.exists() else {}
    if "events" not in data:
        if not info.has_audio:
            data["events"] = []
        else:
            x = media.extract_audio(info.path, cfg.detect.sample_rate)
            data["events"] = [e.time_s for e in audio.detect(x, cfg.detect).events]
    times = data["events"]
    if "new_ink" not in data:
        data["new_ink"], data["ecc_failures"] = None, None
        if cfg.ink.enabled and len(times) >= 2:
            ana = ink.analyse(info.path, info.display_size, cfg.ink)
            data["new_ink"] = [ana.new_ink(a, b, cfg.ink) for a, b in itertools.pairwise(times)]
            data["ecc_failures"] = ana.ecc_failures
            data["ink_frames"] = ana.n
    if want_clean and "clean_time_s" not in data:
        data["clean_time_s"] = ink.clean_frame_time(info.path, info.display_size,
                                                    info.duration_s, cfg.ink)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def _write_strokes(path: Path, labels: events.StrokeLabels) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["index", "start_s", "end_s", "duration_s", "new_ink", "drawing", "long"])
        for s in labels.strokes:
            w.writerow([s.index, f"{s.start_s:.3f}", f"{s.end_s:.3f}", f"{s.duration_s:.3f}",
                        "" if s.new_ink is None else f"{s.new_ink:.1f}",
                        {True: "draw", False: "travel", None: "unknown"}[s.drawing],
                        int(s.long)])


def run_plan(cfg: config.Config) -> int:
    root = cfg.out_dir / "plan"
    root.mkdir(parents=True, exist_ok=True)
    for old in root.glob("clip_*.json"):
        old.unlink()
    sources = probe.order_sources(cfg.sources)
    per_source, labels_all, stats = [], [], []
    for order, info in enumerate(sources):
        data = analyse_source(cfg, info, want_clean=order == len(sources) - 1)
        labels = events.build_strokes(data["events"], data["new_ink"], cfg.strokes)
        _write_strokes(root / f"strokes_{info.path.stem}.csv", labels)
        per_source.append((labels.strokes, info.duration_s))
        labels_all.append(labels)
        long = [s for s in labels.strokes if s.long]
        st = {
            "source": str(info.path),
            "events": len(data["events"]),
            "strokes": len(labels.strokes),
            "long_cutoff_s": round(labels.long_cutoff_s, 3),
            "ink_reliable": labels.ink_reliable,
            "ecc_failures": data.get("ecc_failures"),
            "drawing": sum(s.drawing is True for s in labels.strokes),
            "travel": sum(s.drawing is False for s in labels.strokes),
            "unknown": sum(s.drawing is None for s in labels.strokes),
            "long_drawing": sum(s.drawing is True for s in long),
            "long_travel": sum(s.drawing is False for s in long),
            "clean_time_s": data.get("clean_time_s"),
        }
        stats.append(st)
        print(f"[{order}] {info.path.name}: {st['events']} events, {st['strokes']} strokes "
              f"({st['drawing']} drawing / {st['travel']} travel / {st['unknown']} unknown; "
              f"long >= {st['long_cutoff_s']} s: {st['long_drawing']} drawing, "
              f"{st['long_travel']} travel); ink reliable: {st['ink_reliable']}")

    tls = assign.timelines(per_source)
    result = assign.assign(tls, cfg.segment, cfg.videos)
    last = sources[-1]
    photo = {"path": str(cfg.final_photo.path) if cfg.final_photo.path else None,
             "seconds": cfg.final_photo.seconds,
             "placeholder": {"source": str(last.path), "time_s": stats[-1]["clean_time_s"]}}
    paths = []
    for clip in result.clips:
        data = manifest.clip_manifest(
            cfg.plot, clip, [s.path for s in sources], [lb.strokes for lb in labels_all], photo,
            {"clip_count": result.count,
             "ink_reliable": [lb.ink_reliable for lb in labels_all]})
        path = root / f"clip_{clip.clip + 1:02d}.json"
        manifest.write(path, data)
        paths.append(str(path))
        spans = ", ".join(f"{s.start_s:.2f}-{s.end_s:.2f}" for s in clip.segments)
        print(f"clip {clip.clip + 1}: zone {clip.zone[0]:.1f}-{clip.zone[1]:.1f} s, "
              f"relax {clip.level}, cap {clip.cap_s:g} s, {len(clip.segments)} segments, {clip.total_s:.1f} s: {spans}")
    summary = {"sources": stats, "count": result.count, "tried": result.tried,
               "infeasible_zones": result.infeasible_zones, "manifests": paths,
               "config": {k: asdict(getattr(cfg, k)) for k in
                          ("strokes", "segment", "videos", "ink")}}
    (root / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    if result.infeasible_zones:
        print(f"plotvid: zones {[z + 1 for z in result.infeasible_zones]} of {result.count} "
              f"cannot reach {cfg.videos.min_total_s} s of footage", file=sys.stderr)
    print(f"{len(result.clips)} clip plans -> {root}")
    return 0 if result.clips else 1


def _fps(cfg: config.Config, infos: dict[str, probe.SourceInfo]) -> float:
    if cfg.render.fps:
        return cfg.render.fps
    return render.nice_fps(next(iter(infos.values())).fps)


def run_render(cfg: config.Config) -> int:
    plans = sorted((cfg.out_dir / "plan").glob("clip_*.json"))
    if not plans:
        print("plotvid: no clip manifests; run `plan` first", file=sys.stderr)
        return 1
    infos = {str(i.path): i for i in probe.order_sources(cfg.sources)}
    size = render.output_size([i.display_size for i in infos.values()])
    fps = _fps(cfg, infos)
    assets = cfg.out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    clips_dir = cfg.out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    outro = cfg.outro.path
    if outro is None:
        outro = assets / "outro_card.png"
        render.outro_card(size, cfg.plot, cfg.outro.text, outro)
    outro_part = render.asset_part(outro, cfg.outro.seconds)

    for plan_path in plans:
        data = manifest.read(plan_path)
        photo = cfg.final_photo.path
        if photo is None:
            ph = data["final_photo"]["placeholder"]
            photo = assets / "final_photo.png"
            if not photo.exists() or photo.stat().st_mtime < plan_path.stat().st_mtime:
                render.extract_frame(Path(ph["source"]), ph["time_s"], photo)
        parts = []
        for seg in data["segments"]:
            info = infos.get(seg["source"]) or probe.probe(Path(seg["source"]))
            parts.append(render.Part("segment", info.path, seg["end_s"] - seg["start_s"],
                                     start_s=seg["start_s"], display_size=info.display_size,
                                     has_audio=info.has_audio, fade_audio=True))
        parts.append(render.Part("image", photo, cfg.final_photo.seconds, has_audio=False))
        parts.append(outro_part)
        out = clips_dir / f"{plan_path.stem}.mp4"
        render.render(parts, size, fps, cfg.render, out)
        print(f"{out.name}: {render.expected_duration(parts, cfg.render):.2f} s "
              f"({data['footage_s']:.1f} s footage, {len(data['segments'])} segments) -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="plotvid", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect", help="detect pen events and write hand-check artefacts")
    d.add_argument("config", type=Path)
    d.add_argument("--preview", action="store_true",
                   help="also write preview.mp4: high beep per event, low beep per width-dropped event")
    for name, text in (("plan", "detect, ink check, segments and clip manifests (no rendering)"),
                       ("render", "render the clip manifests written by plan"),
                       ("all", "plan, then render")):
        sub.add_parser(name, help=text).add_argument("config", type=Path)
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
    if args.cmd == "plan":
        return run_plan(cfg)
    if args.cmd == "render":
        return run_render(cfg)
    if args.cmd == "all":
        return run_plan(cfg) or run_render(cfg)
    return 2
