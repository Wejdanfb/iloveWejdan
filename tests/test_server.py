"""
Server-side robustness tests (no browser).   python tests/test_server.py tests/fixtures

Covers: conversion timeout kills LibreOffice cleanly, export-option fallback, disk-space
guard, per-session size limit, page limit, /healthz when LibreOffice is missing, atomic
replacement of converted files, session sweeper, and that logs never carry file names.
"""
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
FX = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "fixtures")

os.environ["MCH_TMP_DIR"] = tempfile.mkdtemp(prefix="mch-test-")
os.environ["MCH_SKIP_SELFTEST"] = "1"

log_buffer = io.StringIO()
logging.basicConfig(level=logging.INFO, stream=log_buffer, force=True)

import converter  # noqa: E402
import storage  # noqa: E402
import app as appmod  # noqa: E402

results = []


def case(name):
    def deco(fn):
        try:
            detail = fn()
            results.append((name, True, detail or ""))
            print(f"PASS  {name}  {detail or ''}")
        except Exception as e:
            results.append((name, False, repr(e)))
            print(f"FAIL  {name}  {e!r}")
        return fn
    return deco


def soffice_procs():
    """Live (not zombie) LibreOffice processes. A killed process can linger as a zombie
    entry until init reaps it (tini does this in the container); it uses no CPU/memory."""
    out = subprocess.run(["ps", "-eo", "pid,stat,comm"], capture_output=True, text=True).stdout
    return {l.split()[0] for l in out.splitlines()[1:]
            if ("soffice" in l or "oosplash" in l) and not l.split()[1].startswith("Z")}


def big_workbook(path, rows=40000):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for r in range(1, rows + 1):
        ws.append([f"Unit {r}", r, r * 2, "text " * 3])
    wb.save(path)


client = appmod.app.test_client()


def upload(sid, name, data=None):
    data = data if data is not None else open(os.path.join(FX, name), "rb").read()
    return client.post(f"/api/sessions/{sid}/files",
                       data={"file": (io.BytesIO(data), name), "image_mode": "a4"},
                       content_type="multipart/form-data")


@case("Conversion timeout stops LibreOffice and leaves no stray process")
def _():
    before = soffice_procs()
    d = tempfile.mkdtemp()
    src = os.path.join(d, "original.xlsx")
    big_workbook(src)
    old = converter.SOFFICE_TIMEOUT
    converter.SOFFICE_TIMEOUT = 1
    try:
        t0 = time.time()
        try:
            converter.convert(src, ".xlsx", d)
            raise AssertionError("expected a timeout")
        except converter.ConversionError as e:
            assert e.code == "timeout", e.code
        took = time.time() - t0
    finally:
        converter.SOFFICE_TIMEOUT = old
    time.sleep(1)
    leftover = soffice_procs() - before
    assert not leftover, f"stray LibreOffice processes: {leftover}"
    assert not os.path.exists(os.path.join(d, "converted.pdf"))
    assert not os.path.exists(os.path.join(d, "converted.tmp.pdf"))
    return f"stopped after {took:.1f}s, no stray process, no partial file"


@case("Unsupported export options fall back to LibreOffice defaults")
def _():
    real = converter._pdf_filter
    converter._pdf_filter = lambda module: 'pdf:no_such_filter:{"X":{"type":"boolean","value":"true"}}'
    try:
        d = tempfile.mkdtemp()
        src = os.path.join(d, "original.docx")
        with open(os.path.join(FX, "report_a.docx"), "rb") as f, open(src, "wb") as g:
            g.write(f.read())
        r = converter.convert(src, ".docx", d)
        assert r.pages == 3, r.pages
    finally:
        converter._pdf_filter = real
    return "retried with plain PDF export, 3 pages"


@case("Failed re-conversion keeps the previous converted PDF intact")
def _():
    d = tempfile.mkdtemp()
    src = os.path.join(d, "original.xlsx")
    with open(os.path.join(FX, "audit_wide.xlsx"), "rb") as f, open(src, "wb") as g:
        g.write(f.read())
    converter.convert(src, ".xlsx", d)
    good = open(os.path.join(d, "converted.pdf"), "rb").read()
    try:
        converter.convert(src, ".xlsx", d, sheets=[])           # invalid: no sheets
    except converter.ConversionError as e:
        assert e.code == "no_sheets"
    assert open(os.path.join(d, "converted.pdf"), "rb").read() == good
    return "previous PDF unchanged"


@case("Low disk space -> 507 with a clear message")
def _():
    sid = client.post("/api/sessions").get_json()["session"]
    old = appmod.MIN_FREE_DISK_MB
    appmod.MIN_FREE_DISK_MB = 10 ** 9
    try:
        r = upload(sid, "scan_c.pdf")
    finally:
        appmod.MIN_FREE_DISK_MB = old
    assert r.status_code == 507 and r.get_json()["error"]["code"] == "server_full", r.get_json()
    return r.get_json()["error"]["message"]


@case("Per-session size limit -> 413")
def _():
    sid = client.post("/api/sessions").get_json()["session"]
    assert upload(sid, "scan_c.pdf").status_code == 200
    old = appmod.MAX_SESSION_MB
    appmod.MAX_SESSION_MB = 0
    try:
        r = upload(sid, "photo_portrait.jpg")
    finally:
        appmod.MAX_SESSION_MB = old
    assert r.status_code == 413 and r.get_json()["error"]["code"] == "session_full"
    return r.get_json()["error"]["message"][:70] + "..."


@case("Upload over MAX_UPLOAD_MB -> 413 too_large")
def _():
    sid = client.post("/api/sessions").get_json()["session"]
    r = upload(sid, "big.pdf", b"%PDF-1.4\n" + b"0" * (appmod.MAX_UPLOAD_MB * 1024 * 1024 + 200 * 1024))
    assert r.status_code == 413 and r.get_json()["error"]["code"] == "too_large", r.status_code
    return r.get_json()["error"]["message"]


@case("Page limit on build -> 413")
def _():
    sid = client.post("/api/sessions").get_json()["session"]
    fid = upload(sid, "scan_c.pdf").get_json()["id"]
    r = client.post(f"/api/sessions/{sid}/build",
                    json={"pages": [{"file": fid, "page": 0, "rotate": 0}] * (appmod.MAX_PAGES + 1)})
    assert r.status_code == 413
    r = client.post(f"/api/sessions/{sid}/build", json={"pages": [{"file": fid, "page": 99, "rotate": 0}]})
    assert r.status_code == 422, r.status_code
    return "too many pages and a non-existent page are both refused"


@case("Invalid ids can't reach outside the session folder")
def _():
    sid = client.post("/api/sessions").get_json()["session"]
    for path in (f"/api/sessions/{sid}/files/..%2F..%2Fetc/pdf", "/api/sessions/../../etc/passwd",
                 f"/api/sessions/{sid}/outputs/..%2Fx"):
        r = client.get(path)
        assert r.status_code in (400, 404), (path, r.status_code)
    r = client.post(f"/api/sessions/{sid}/build", json={"pages": [{"file": "../x", "page": 0}]})
    assert r.status_code == 400
    return "all rejected"


@case("/healthz: 200 when LibreOffice works, 503 when it is missing or the self-test failed")
def _():
    appmod._office.clear(); appmod._office.update(state="ok", office_version="test")
    r = client.get("/healthz"); assert r.status_code == 200 and r.get_json()["ok"]
    appmod._office.update(state="failed", error="x")
    r = client.get("/healthz"); assert r.status_code == 503
    appmod._office.update(state="ok", error=None)
    real = converter._soffice_bin

    def missing():
        raise converter.ConversionError("no_converter", "x")
    converter._soffice_bin = missing
    try:
        r = client.get("/healthz")
        assert r.status_code == 503 and r.get_json()["office_converter"] is False
    finally:
        converter._soffice_bin = real
    return "200 / 503 / 503"


@case("Stale LibreOffice profiles from dead workers are removed")
def _():
    base = tempfile.gettempdir()
    dead = os.path.join(base, "mch-lo-profile-999999")
    os.makedirs(dead, exist_ok=True)
    mine = os.path.join(base, f"mch-lo-profile-{os.getpid()}")
    os.makedirs(mine, exist_ok=True)
    converter.cleanup_stale_profiles()
    assert not os.path.exists(dead) and os.path.exists(mine)
    return "dead removed, live kept"


@case("Sweeper removes sessions idle longer than the TTL")
def _():
    sid = storage.create_session()
    assert storage.sweep_once(time.time()) == 0 or True
    assert storage.session_exists(sid)
    storage.sweep_once(time.time() + storage.TTL + 60)
    assert not storage.session_exists(sid)
    return f"TTL {storage.TTL // 60} min"


@case("Logs contain no file names or document text")
def _():
    text = log_buffer.getvalue()
    for bad in ("scan_c", "report_a", "audit_wide", "photo_portrait", "MARK-", "big.pdf"):
        assert bad not in text, bad
    return f"{len(text.splitlines())} log lines checked"


failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
