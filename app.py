"""
MCH PDF Organizer - web application.

Run locally:   python app.py        -> binds HOST (default 0.0.0.0) and PORT (default 5052)
On Render:     gunicorn -c gunicorn.conf.py app:app   (see Dockerfile; Render sets PORT)

All document processing happens on this server (see converter.py / pdf_ops.py).
Files are kept in a temporary per-session folder and deleted on "Start over",
after SESSION_TTL_MINUTES of inactivity, or when the server restarts.
No external conversion service is used.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

import converter
import pdf_ops
import storage

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
MAX_SESSION_MB = int(os.environ.get("MAX_SESSION_MB", "300"))     # all files of one user together
MIN_FREE_DISK_MB = int(os.environ.get("MIN_FREE_DISK_MB", "200"))  # refuse uploads below this
MAX_PAGES = int(os.environ.get("MAX_PAGES", "3000"))               # pages in one combined PDF
DEFAULT_NAME = "MCH_Combined_Document"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Request logging could reveal URLs only (ids, never names), but keep it quiet anyway.
logging.getLogger("werkzeug").setLevel(logging.WARNING)
log = logging.getLogger("mch.app")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024 + 64 * 1024

# LibreOffice check at start-up: runs a real Word + Excel conversion in the background
# (also warms up LibreOffice so the first user doesn't wait for it). /healthz reports it.
_office = {"state": "pending"}


def _startup_checks():
    try:
        removed = converter.cleanup_stale_profiles()
        if removed:
            log.info("removed %d stale LibreOffice profile(s)", removed)
    except Exception:
        log.exception("profile cleanup failed")
    result = converter.self_test()
    _office.update(result, state="ok" if result["ok"] else "failed")
    if result["ok"]:
        log.info("office self-test passed (%s)", result["office_version"])
    else:
        log.error("office self-test FAILED: %s (version=%s)", result.get("error"), result.get("office_version"))


_bg_pid = None
_bg_lock = threading.Lock()


def ensure_background_tasks():
    """Start the session sweeper and the LibreOffice check once per *process*.

    Also correct if the app is imported before gunicorn forks (e.g. `--preload`): threads
    don't survive a fork, so the worker starts its own on its first request."""
    global _bg_pid
    if _bg_pid == os.getpid():
        return
    with _bg_lock:
        if _bg_pid == os.getpid():
            return
        _bg_pid = os.getpid()
        storage.start_sweeper()
        if os.environ.get("MCH_SKIP_SELFTEST") != "1":
            _office.clear()
            _office.update(state="pending")
            threading.Thread(target=_startup_checks, name="mch-selftest", daemon=True).start()
        else:
            _office.update(state="skipped")


ensure_background_tasks()


@app.before_request
def _per_process_start():
    ensure_background_tasks()


# ---------------------------------------------------------------- helpers

def api_error(status: int, code: str, message: str):
    resp = jsonify({"error": {"code": code, "message": message}})
    resp.status_code = status
    return resp


def need_session(sid: str) -> None:
    if not storage.session_exists(sid):
        abort(api_error(404, "session_expired",
                        "Your session on the server has expired or was cleared. Please start over and add the files again."))
    storage.touch(sid)


def clean_filename(raw: str | None) -> str:
    name = (raw or "").strip()
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name)
    name = name.strip(" .")
    return (name[:150].strip(" .")) or DEFAULT_NAME


def display_name(raw: str) -> str:
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name[:200] or "file"


def public_meta(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if not k.startswith("_")}


def run_conversion(sid: str, fid: str, meta: dict, sheets=None, image_mode="a4") -> dict:
    fdir = storage.file_dir(sid, fid)
    src = os.path.join(fdir, "original" + meta["ext"])
    res = converter.convert(src, meta["ext"], fdir, sheets=sheets, image_mode=image_mode)
    meta.update({
        "status": "ready",
        "pages": res.pages,
        "sizes": res.page_sizes,
        "sheets": res.sheets,
        "notes": res.notes,
        "image_mode": image_mode if meta["kind"] == "image" else None,
        "version": meta.get("version", 0) + 1,
    })
    storage.write_meta(sid, fid, meta)
    return meta


# ---------------------------------------------------------------- pages

@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    return render_template("index.html", max_upload_mb=MAX_UPLOAD_MB, ttl_minutes=storage.TTL // 60,
                           accept=",".join(converter.SUPPORTED_EXTENSIONS))


@app.get("/healthz")
def healthz():
    """200 while healthy (or while the start-up check is still running);
    503 if LibreOffice is missing or the start-up conversion test failed."""
    try:
        converter._soffice_bin()
        found = True
    except converter.ConversionError:
        found = False
    try:
        free_mb = shutil.disk_usage(storage.ROOT if os.path.isdir(storage.ROOT) else "/").free // (1024 * 1024)
    except OSError:
        free_mb = None
    state = _office.get("state")
    healthy = found and state in ("pending", "ok", "skipped")
    body = {
        "ok": healthy,
        "office_converter": found,
        "office_version": _office.get("office_version"),
        "office_selftest": state,
        "office_error": _office.get("error"),
        "free_disk_mb": free_mb,
        "max_upload_mb": MAX_UPLOAD_MB,
    }
    resp = jsonify(body)
    resp.status_code = 200 if healthy else 503
    return resp


# ---------------------------------------------------------------- sessions

@app.post("/api/sessions")
def create_session():
    return jsonify({"session": storage.create_session(), "max_upload_mb": MAX_UPLOAD_MB})


@app.get("/api/sessions/<sid>")
def get_session(sid):
    need_session(sid)
    return jsonify({"files": [public_meta(m) for m in storage.list_files(sid)]})


@app.delete("/api/sessions/<sid>")
def delete_session(sid):
    if storage.valid_id(sid):
        storage.delete_session(sid)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- files

@app.post("/api/sessions/<sid>/files")
def upload(sid):
    need_session(sid)
    f = request.files.get("file")
    if f is None or not f.filename:
        return api_error(400, "no_file", "No file was received.")
    name = display_name(f.filename)
    ext = os.path.splitext(name.lower())[1]
    kind = converter.kind_for(name)
    if kind is None:
        return api_error(415, "unsupported",
                         "This file type isn't supported. Use Excel (.xlsx, .xls), Word (.docx, .doc), PDF, PNG or JPG.")

    incoming = request.content_length or 0
    try:
        free = shutil.disk_usage(storage.ROOT).free
    except OSError:
        free = None
    if free is not None and free - incoming < MIN_FREE_DISK_MB * 1024 * 1024:
        log.warning("upload refused: low disk space")
        return api_error(507, "server_full",
                         "The server is temporarily out of storage space. Please try again in a few minutes.")
    if storage.session_bytes(sid) + incoming > MAX_SESSION_MB * 1024 * 1024:
        return api_error(413, "session_full",
                         f"This session already holds close to {MAX_SESSION_MB} MB of files. "
                         "Export what you have, or remove some files, before adding more.")

    fid = storage.new_id(12)
    fdir = storage.file_dir(sid, fid)
    os.makedirs(fdir)
    src = os.path.join(fdir, "original" + ext)
    f.save(src)
    h = hashlib.sha256()
    with open(src, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)

    meta = {"id": fid, "name": name, "ext": ext, "kind": kind, "size": os.path.getsize(src),
            "sha": h.hexdigest()[:16], "created": time.time(), "status": "converting", "version": 0}
    storage.write_meta(sid, fid, meta)
    image_mode = request.form.get("image_mode", "a4")
    image_mode = image_mode if image_mode in ("a4", "original") else "a4"
    try:
        meta = run_conversion(sid, fid, meta, image_mode=image_mode)
    except converter.ConversionError as e:
        storage.delete_file(sid, fid)
        log.info("conversion refused kind=%s code=%s", kind, e.code)
        return api_error(422, e.code, e.message)
    except Exception:
        storage.delete_file(sid, fid)
        log.exception("conversion crashed kind=%s", kind)
        return api_error(500, "failed", "The file couldn't be converted because of an unexpected error.")
    return jsonify(public_meta(meta))


@app.post("/api/sessions/<sid>/files/<fid>/reconvert")
def reconvert(sid, fid):
    need_session(sid)
    meta = storage.read_meta(sid, fid)
    if not meta:
        return api_error(404, "missing", "That file is no longer on the server.")
    body = request.get_json(silent=True) or {}
    sheets = body.get("sheets") if meta["kind"] == "excel" else None
    if sheets is not None and (not isinstance(sheets, list) or not all(isinstance(s, str) for s in sheets)):
        return api_error(400, "bad_request", "Invalid sheet list.")
    image_mode = body.get("image_mode", meta.get("image_mode") or "a4")
    image_mode = image_mode if image_mode in ("a4", "original") else "a4"
    try:
        meta = run_conversion(sid, fid, meta, sheets=sheets, image_mode=image_mode)
    except converter.ConversionError as e:
        return api_error(422, e.code, e.message)
    except Exception:
        log.exception("reconversion crashed kind=%s", meta.get("kind"))
        return api_error(500, "failed", "The file couldn't be converted because of an unexpected error.")
    return jsonify(public_meta(meta))


@app.delete("/api/sessions/<sid>/files/<fid>")
def remove_file(sid, fid):
    need_session(sid)
    if storage.valid_id(fid):
        storage.delete_file(sid, fid)
    return jsonify({"ok": True})


@app.get("/api/sessions/<sid>/files/<fid>/pdf")
def file_pdf(sid, fid):
    need_session(sid)
    if not storage.valid_id(fid):
        abort(404)
    path = os.path.join(storage.file_dir(sid, fid), "converted.pdf")
    if not os.path.exists(path):
        return api_error(404, "missing", "That file is no longer on the server.")
    return send_file(path, mimetype="application/pdf", conditional=False, max_age=0)


# ---------------------------------------------------------------- export

@app.post("/api/sessions/<sid>/build")
def build(sid):
    need_session(sid)
    body = request.get_json(silent=True) or {}
    seq = body.get("pages")
    if not isinstance(seq, list):
        return api_error(400, "bad_request", "Invalid page list.")
    if len(seq) > MAX_PAGES:
        return api_error(413, "too_many_pages", f"A combined PDF can have at most {MAX_PAGES} pages.")
    sources = {}
    for item in seq:
        fid = item.get("file") if isinstance(item, dict) else None
        if not storage.valid_id(fid):
            return api_error(400, "bad_request", "Invalid page list.")
        if fid not in sources:
            p = os.path.join(storage.file_dir(sid, fid), "converted.pdf")
            if not os.path.exists(p):
                return api_error(404, "missing", "One of the documents is no longer on the server. Please re-add it.")
            sources[fid] = p
    filename = clean_filename(body.get("filename"))
    storage.clear_outputs(sid)
    oid = storage.new_id(12)
    out = storage.output_path(sid, oid)
    try:
        n = pdf_ops.build(seq, sources, out, title=filename)
    except pdf_ops.BuildError as e:
        return api_error(422, "build_failed", str(e))
    except Exception:
        log.exception("build crashed pages=%d", len(seq))
        return api_error(500, "build_failed", "The combined PDF couldn't be created because of an unexpected error.")
    with open(out[:-4] + ".json", "w", encoding="utf-8") as fh:
        json.dump({"filename": filename}, fh)
    log.info("built output pages=%d bytes=%d", n, os.path.getsize(out))
    return jsonify({"output": oid, "pages": n, "size": os.path.getsize(out), "filename": filename + ".pdf"})


@app.post("/api/sessions/<sid>/outputs/<oid>/name")
def rename_output(sid, oid):
    need_session(sid)
    out = storage.output_path(sid, oid)
    if not os.path.exists(out):
        return api_error(404, "missing", "The combined PDF is no longer on the server. Please build it again.")
    filename = clean_filename((request.get_json(silent=True) or {}).get("filename"))
    with open(out[:-4] + ".json", "w", encoding="utf-8") as fh:
        json.dump({"filename": filename}, fh)
    return jsonify({"filename": filename + ".pdf"})


@app.get("/api/sessions/<sid>/outputs/<oid>")
def get_output(sid, oid):
    need_session(sid)
    try:
        out = storage.output_path(sid, oid)
    except KeyError:
        abort(404)
    if not os.path.exists(out):
        return api_error(404, "missing", "The combined PDF is no longer on the server. Please build it again.")
    try:
        with open(out[:-4] + ".json", encoding="utf-8") as fh:
            filename = json.load(fh)["filename"]
    except (OSError, ValueError, KeyError):
        filename = DEFAULT_NAME
    download = request.args.get("download") == "1"
    return send_file(out, mimetype="application/pdf", as_attachment=download,
                     download_name=filename + ".pdf", conditional=False, max_age=0)


# ---------------------------------------------------------------- errors

@app.errorhandler(RequestEntityTooLarge)
def too_large(_e):
    return api_error(413, "too_large", f"The file is larger than the {MAX_UPLOAD_MB} MB limit.")


@app.errorhandler(HTTPException)
def http_error(e):
    if request.path.startswith("/api/"):
        if e.response is not None:
            return e.response
        return api_error(e.code or 500, "http_error", e.description or "Request failed.")
    return e


if __name__ == "__main__":
    # Development / local use. Render runs gunicorn instead (gunicorn.conf.py), which also
    # binds 0.0.0.0:$PORT. run.bat sets HOST=127.0.0.1 so a desktop copy stays private.
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5052"))
    app.run(host=host, port=port, debug=False, threaded=True)
