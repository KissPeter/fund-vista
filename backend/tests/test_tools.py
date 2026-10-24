"""The tiny ops tools (backend/tools) over a real process + real HTTP.

``backend/tools/json_field.py``, ``http_get.py`` and ``untar_stream.py``
replace the inline ``python -c '...'`` snippets the ops scripts used to
carry (issue #64). They are exercised the way the shell scripts use them:
as a module in a subprocess with stdin/stdout wiring — never through
FastAPI's TestClient.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(module: str, args: list[str], stdin: bytes = b"") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        input=stdin,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        timeout=60,
    )


# -- json_field --------------------------------------------------------------

def test_json_field_reads_nested_paths():
    doc = json.dumps({"job_id": "abc", "error": {"code": "invalid_params"},
                      "items": [{"id": "x1"}], "status": "done"}).encode()
    proc = _run("backend.tools.json_field", ["job_id"], doc)
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().strip() == "abc"

    proc = _run("backend.tools.json_field", ["error.code"], doc)
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().strip() == "invalid_params"

    proc = _run("backend.tools.json_field", ["items.0.id"], doc)
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().strip() == "x1"


def test_json_field_missing_path_and_broken_body():
    doc = json.dumps({"status": "done"}).encode()
    proc = _run("backend.tools.json_field", ["error.code"], doc)
    assert proc.returncode == 1
    assert proc.stdout.decode() == ""

    proc = _run("backend.tools.json_field", ["status"], b"<html>502</html>")
    assert proc.returncode == 1
    assert proc.stdout.decode() == ""

    proc = _run("backend.tools.json_field", ["status"], b"")
    assert proc.returncode == 1


def test_json_field_usage_error():
    proc = _run("backend.tools.json_field", [])
    assert proc.returncode == 2
    assert "usage" in proc.stderr.decode()


def test_json_field_object_value_is_json():
    doc = json.dumps({"error": {"code": "x", "message": "y"}}).encode()
    proc = _run("backend.tools.json_field", ["error"], doc)
    assert proc.returncode == 0, proc.stderr.decode()
    assert json.loads(proc.stdout.decode()) == {"code": "x", "message": "y"}


# -- http_get ----------------------------------------------------------------

def test_http_get_reads_a_real_body(live_server):
    proc = _run("backend.tools.http_get", [f"{live_server}/healthz"])
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().strip()


def test_http_get_fails_on_a_dead_port():
    # Port 1 on the loopback: nothing listens there -> non-zero exit, no body.
    proc = _run("backend.tools.http_get", ["http://127.0.0.1:1/healthz"])
    assert proc.returncode == 1
    assert proc.stdout.decode() == ""


def test_http_get_usage_error():
    proc = _run("backend.tools.http_get", [])
    assert proc.returncode == 2
    assert "usage" in proc.stderr.decode()


# -- untar_stream ------------------------------------------------------------

def _tar_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def test_untar_stream_extracts_a_plain_archive(tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    proc = _run("backend.tools.untar_stream", [str(dest)],
                _tar_bytes([("a.txt", b"hello"), ("sub/b.txt", b"world")]))
    assert proc.returncode == 0, proc.stderr.decode()
    assert (dest / "a.txt").read_bytes() == b"hello"
    assert (dest / "sub" / "b.txt").read_bytes() == b"world"


def test_untar_stream_refuses_path_traversal(tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    proc = _run("backend.tools.untar_stream", [str(dest)],
                _tar_bytes([("../escape.txt", b"nope")]))
    assert proc.returncode == 1
    assert not (tmp_path / "escape.txt").exists()
