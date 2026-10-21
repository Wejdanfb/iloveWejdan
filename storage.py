"""
Temporary, per-session file storage for MCH PDF Organizer.

Layout:  <root>/<session>/files/<file_id>/{original.<ext>, converted.pdf, meta.json}
         <root>/<session>/outputs/<output_id>.pdf (+ .json)

Nothing here is permanent. A session is removed when the user presses
"Start over", and a background sweeper removes any session that has not been
used for SESSION_TTL_MINUTES (default 120). The whole folder also disappears
whenever the server restarts on Render (its disk is not persistent).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time

log = logging.getLogger("mch.storage")

ROOT = os.environ.get("MCH_TMP_DIR") or os.path.join(tempfile.gettempdir(), "mch-pdf-organizer")
TTL = int(os.environ.get("SESSION_TTL_MINUTES", "120")) * 60
_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def new_id(n: int = 16) -> str:
    return secrets.token_urlsafe(n)


def valid_id(value: str | None) -> bool:
    return bool(value) and bool(_ID.match(value))


def session_dir(sid: str) -> str:
    if not valid_id(sid):
        raise KeyError("bad id")
    return os.path.join(ROOT, sid)


def create_session() -> str:
    sid = new_id()
    os.makedirs(os.path.join(session_dir(sid), "files"), exist_ok=True)
    os.makedirs(os.path.join(session_dir(sid), "outputs"), exist_ok=True)
    return sid


def session_exists(sid: str) -> bool:
    try:
        return os.path.isdir(session_dir(sid))
    except KeyError:
        return False


def touch(sid: str) -> None:
    try:
        os.utime(session_dir(sid), None)
    except OSError:
        pass


def delete_session(sid: str) -> None:
    shutil.rmtree(session_dir(sid), ignore_errors=True)


def file_dir(sid: str, fid: str) -> str:
    if not valid_id(fid):
        raise KeyError("bad id")
    return os.path.join(session_dir(sid), "files", fid)


def write_meta(sid: str, fid: str, meta: dict) -> None:
    path = os.path.join(file_dir(sid, fid), "meta.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp, path)


def read_meta(sid: str, fid: str) -> dict | None:
    try:
        with open(os.path.join(file_dir(sid, fid), "meta.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, KeyError):
        return None


def list_files(sid: str) -> list[dict]:
    base = os.path.join(session_dir(sid), "files")
    out = []
    for fid in os.listdir(base) if os.path.isdir(base) else []:
        m = read_meta(sid, fid)
        if m:
            out.append(m)
    out.sort(key=lambda m: m.get("created", 0))
    return out


def session_bytes(sid: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(session_dir(sid)):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def delete_file(sid: str, fid: str) -> None:
    shutil.rmtree(file_dir(sid, fid), ignore_errors=True)


def output_path(sid: str, oid: str) -> str:
    if not valid_id(oid):
        raise KeyError("bad id")
    return os.path.join(session_dir(sid), "outputs", oid + ".pdf")


def clear_outputs(sid: str) -> None:
    d = os.path.join(session_dir(sid), "outputs")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)


def sweep_once(now: float | None = None) -> int:
    now = now or time.time()
    removed = 0
    if not os.path.isdir(ROOT):
        return 0
    for name in os.listdir(ROOT):
        p = os.path.join(ROOT, name)
        try:
            if os.path.isdir(p) and now - os.path.getmtime(p) > TTL:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    if removed:
        log.info("sweeper removed %d expired session(s)", removed)
    return removed


_sweeper_pid = None


def start_sweeper(interval: int = 600) -> None:
    """Start the cleanup thread once per process (threads don't survive a fork)."""
    global _sweeper_pid
    if _sweeper_pid == os.getpid():
        return
    _sweeper_pid = os.getpid()
    os.makedirs(ROOT, exist_ok=True)

    def loop():
        while True:
            try:
                sweep_once()
            except Exception:
                log.exception("sweeper error")
            time.sleep(interval)

    threading.Thread(target=loop, name="mch-sweeper", daemon=True).start()
