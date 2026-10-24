"""Profile the parent of the parallel archive pass (issue #66 follow-up 3).

Builds a synthetic tar archive with the adsb.lol member layout
(``trace_full_*.json.gz``), runs ``extract_multi(workers=N)`` in this
process and reports how much CPU the parent owns versus the worker pool.

The parent = the process that reads the tar and ships member bytes to the
pool. The pool = the worker processes (a ``forkserver`` on Linux, so they
are *not* this process' children — the container's cgroup CPU counter is
the reliable total there).

Run inside the production container (the Mac host's py-spy needs root):

    docker run --rm -v "$PWD:/srv/fund-vista" -w /srv/fund-vista \\
      -e PYTHONPATH=/srv/fund-vista fundvista:mac \\
      sh -c "pip install --quiet py-spy; py-spy record --rate 100 \\
             --output /tmp/parent.svg -- python backend/tools/profile_ingest_parent.py"
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import resource
import sys
import tarfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.traffic.ingest import default_bbox, extract_multi  # noqa: E402

SPECS = [{"icao": "LHBP", "lat": 47.4369, "lon": 19.2556, "elev_ft": 495.0,
          "bbox": default_bbox(47.4369, 19.2556)}]


def _doc(i: int) -> dict:
    """A doc with a realistic trace: 120 points, every 8th one on the ground."""
    trace = []
    for p in range(120):
        t = p * 2.0
        lat = 47.4369 + 0.001 * ((p % 30) - 15)
        lon = 19.2556 + 0.001 * ((p % 17) - 8)
        if p % 8 == 0:
            trace.append([t, lat, lon, "ground"])
        else:
            trace.append([t, lat, lon, 3000 + (p % 11) * 120])
    return {"icao": f"aabb{i:04x}", "r": "HA-ABC", "t": "C172",
            "timestamp": 1760000000, "trace": trace}


def build_archive(n_docs: int) -> bytes:
    raw = io.BytesIO()
    tf = tarfile.open(fileobj=raw, mode="w")
    for i in range(n_docs):
        payload = gzip.compress(json.dumps(_doc(i)).encode("utf-8"))
        ti = tarfile.TarInfo(f"traces/{i % 256:02x}/trace_full_aabb{i:04x}.json")
        ti.size = len(payload)
        tf.addfile(ti, io.BytesIO(payload))
    tf.close()
    return raw.getvalue()


def _self_cpu() -> float:
    me = resource.getrusage(resource.RUSAGE_SELF)
    return me.ru_utime + me.ru_stime


def _cgroup_cpu_s() -> float | None:
    """Container CPU seconds (cgroup v2), or None outside a cgroup v2 host."""
    for path in ("/sys/fs/cgroup/cpu.stat",):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("usage_usec"):
                        return int(line.split()[1]) / 1_000_000
        except OSError:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=20000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--batch", type=int, default=200)
    args = ap.parse_args()

    blob = build_archive(args.docs)
    print(f"archive: {len(blob) / 1e6:.1f} MB, {args.docs} members, workers={args.workers}")

    cpu0 = _self_cpu()
    cg0 = _cgroup_cpu_s()
    t0 = time.time()
    out = extract_multi(io.BytesIO(blob), SPECS, workers=args.workers, batch_members=args.batch)
    wall = time.time() - t0
    cpu1 = _self_cpu()
    cg1 = _cgroup_cpu_s()

    parent_cpu = cpu1 - cpu0
    kept = len(out["LHBP"])
    print(f"members parsed: {args.docs}, kept: {kept}")
    print(f"wall: {wall:.2f}s")
    print(f"parent cpu: {parent_cpu:.2f}s")
    if cg0 is not None and cg1 is not None:
        total = cg1 - cg0
        print(f"cgroup cpu (parent + pool): {total:.2f}s")
        print(f"parent share of cpu: {100 * parent_cpu / total:.1f}%")
        print(f"parallel efficiency: {total / wall:.2f} cpu-seconds per wall-second")
    else:
        kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        print(f"child cpu: {kids.ru_utime + kids.ru_stime:.2f}s "
              "(no cgroup counter — pool children may not be counted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
