"""
API smoke test for a running MCH PDF Organizer (local, Docker or the live Render URL).
Uses only the Python standard library (pypdf is used for extra checks if installed).

  python tests/smoke_api.py https://your-service.onrender.com tests/fixtures
  python tests/smoke_api.py http://127.0.0.1:10000 tests/fixtures --keep-session   # prints session id
  python tests/smoke_api.py http://127.0.0.1:10000 tests/fixtures --expect-expired <session id>

Uploads one file of every supported format, checks page counts and error messages for
bad files, re-selects Excel sheets, builds a combined PDF in a custom order with a
rotation, downloads it, then deletes the session. Exit code 0 = all checks passed.
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

GOOD = {                    # file -> expected pages
    "report_a.docx": 3,
    "policy_legacy.doc": 2,
    "audit_wide.xlsx": 2,
    "summary_legacy.xls": 1,
    "scan_c.pdf": 4,
    "chart_landscape.png": 1,
    "photo_portrait.jpg": 1,
    "phone_exif.jpeg": 1,
}
BAD = {                     # file -> (HTTP status, error code)
    "locked.pdf": (422, "password"),
    "not_really.docx": (422, "corrupt"),
    "notes.txt": (415, "unsupported"),
}

failures = []


def check(cond, label):
    print(("  ok    " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)
    return cond


def call(base, method, path, body=None, raw=False, timeout=300):
    data, headers = None, {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            return r.status, (payload if raw else json.loads(payload or b"null")), dict(r.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload), dict(e.headers)
        except ValueError:
            return e.code, payload, dict(e.headers)


def upload(base, sid, path):
    boundary = uuid.uuid4().hex
    name = os.path.basename(path)
    with open(path, "rb") as f:
        content = f.read()
    body = io.BytesIO()
    body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
               f"Content-Type: application/octet-stream\r\n\r\n".encode())
    body.write(content)
    body.write(f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"image_mode\"\r\n\r\na4"
               f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"{base}/api/sessions/{sid}/files", data=body.getvalue(), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_healthy(base, limit=240):
    print(f"Waiting for {base}/healthz (LibreOffice start-up self-test) ...")
    t0 = time.time()
    last = None
    while time.time() - t0 < limit:
        try:
            status, body, _ = call(base, "GET", "/healthz", timeout=15)
            last = (status, body)
            if status == 200 and body.get("office_selftest") in ("ok", "skipped"):
                return body
            if status == 503:
                break
        except Exception as e:
            last = repr(e)
        time.sleep(3)
    print("  last health response:", last)
    return None


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    base, fx = sys.argv[1].rstrip("/"), sys.argv[2]
    args = sys.argv[3:]

    health = wait_healthy(base)
    check(health is not None, "service healthy and LibreOffice self-test passed")
    if health is None:
        sys.exit(1)
    print("  LibreOffice:", health.get("office_version"), "| free disk MB:", health.get("free_disk_mb"))

    if "--expect-expired" in args:
        sid = args[args.index("--expect-expired") + 1]
        status, body, _ = call(base, "GET", f"/api/sessions/{sid}")
        check(status == 404 and body["error"]["code"] == "session_expired",
              "after restart the old session is gone and reported as expired")
        status, body, _ = call(base, "POST", f"/api/sessions/{sid}/build", {"pages": []})
        check(status == 404, "building from an expired session is refused cleanly")
        sys.exit(1 if failures else 0)

    status, s, _ = call(base, "POST", "/api/sessions")
    check(status == 200, "create session")
    sid = s["session"]

    print("Converting one file of each format:")
    ids = {}
    for name, pages in GOOD.items():
        t0 = time.time()
        status, meta = upload(base, sid, os.path.join(fx, name))
        ok = status == 200 and meta.get("pages") == pages
        check(ok, f"{name:22s} -> {meta.get('pages', meta)} page(s) in {time.time() - t0:.1f}s")
        if status == 200:
            ids[name] = meta

    print("Bad files are refused with a clear message:")
    for name, (code, err) in BAD.items():
        status, body = upload(base, sid, os.path.join(fx, name))
        e = body.get("error", {})
        check(status == code and e.get("code") == err and len(e.get("message", "")) > 10,
              f"{name:22s} -> {status} {e.get('code')}: {e.get('message')}")

    print("Excel sheet selection:")
    x = ids.get("audit_wide.xlsx")
    if x:
        status, meta, _ = call(base, "POST", f"/api/sessions/{sid}/files/{x['id']}/reconvert", {"sheets": ["Notes"]})
        check(status == 200 and meta["pages"] == 1, f"only sheet 'Notes' -> {meta.get('pages')} page")
        ids["audit_wide.xlsx"] = meta

    print("Combined PDF in a custom order:")
    seq = []
    for name in reversed(list(ids)):
        m = ids[name]
        for i in range(m["pages"]):
            seq.append({"file": m["id"], "page": i, "rotate": 0})
    # rotate the first page of scan_c.pdf (a plain, unrotated source page) by 90 degrees
    rot_at = next(i for i, p in enumerate(seq) if p["file"] == ids["scan_c.pdf"]["id"])
    seq[rot_at]["rotate"] = 90
    status, out, _ = call(base, "POST", f"/api/sessions/{sid}/build",
                          {"pages": seq, "filename": "Smoke Test: تقرير"})
    check(status == 200 and out["pages"] == len(seq), f"build -> {out.get('pages')} pages (expected {len(seq)})")
    status, pdf, headers = call(base, "GET", f"/api/sessions/{sid}/outputs/{out['output']}?download=1", raw=True)
    check(status == 200 and pdf[:5] == b"%PDF-" and b"%%EOF" in pdf[-1024:], f"download is a complete PDF ({len(pdf):,} bytes)")
    cd = headers.get("Content-Disposition", "")
    check("attachment" in cd and "Smoke%20Test" in cd, "download file name cleaned and kept (incl. Arabic)")
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(pdf))
        check(len(r.pages) == len(seq), f"PDF opens with {len(r.pages)} pages")
        check((r.pages[rot_at].rotation or 0) % 360 == 90, f"page {rot_at + 1} rotated 90 degrees as requested")
    except ImportError:
        print("  (pypdf not installed - skipped page-level checks)")

    if "--keep-session" in args:
        print(sid)          # last line: session id for --expect-expired
        sys.exit(1 if failures else 0)

    print("Clean-up:")
    call(base, "DELETE", f"/api/sessions/{sid}")
    status, _, _ = call(base, "GET", f"/api/sessions/{sid}")
    check(status == 404, "session and its files deleted from the server")

    print()
    print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
