"""
End-to-end acceptance tests for MCH PDF Organizer (real browser + real server).

  python tests/make_fixtures.py <fixtures>
  BASE=http://127.0.0.1:5052 FX=<fixtures> OUT=<dir> python tests/test_e2e.py

Every downloaded PDF is checked page by page: each output page must be the exact
source page the user put in that position (same content stream and images), with the
expected /Rotate. Text markers are also read back for a human-readable trace.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import traceback
import urllib.request

from playwright.sync_api import sync_playwright
from pypdf import PdfReader

BASE = os.environ.get("BASE", "http://127.0.0.1:5052")
FX = os.environ["FX"]
OUT = os.environ.get("OUT", "e2e-out")
os.makedirs(OUT, exist_ok=True)
STORE = "mch-pdf-organizer-v1"
results = []


def fx(*names):
    return [os.path.join(FX, n) for n in names]


# ------------------------------------------------------------------ helpers

def wait_idle(pg):
    pg.wait_for_function("() => !document.querySelector('.status-badge.pending')", timeout=240000)


def st(pg):
    return pg.evaluate(f"JSON.parse(sessionStorage.getItem('{STORE}'))")


def upload(pg, names):
    pg.set_input_files("#file-input", fx(*names))
    pg.wait_for_timeout(300)
    wait_idle(pg)


def to_step(pg, n):
    pg.click(f"#step-btn-{n}")
    if n == 2:
        pg.wait_for_selector("#panel-organize:not([hidden])")
    if n == 3:
        pg.wait_for_selector("#export-ready:not([hidden])", timeout=120000)


def download(pg, name=None, tag="out"):
    to_step(pg, 3)
    if name is not None:
        pg.fill("#output-filename", name)
    with pg.expect_download(timeout=60000) as d:
        pg.click("#btn-download")
    dl = d.value
    path = os.path.join(OUT, f"{tag}.pdf")
    dl.save_as(path)
    return path, dl.suggested_filename


def page_sig(page):
    h = hashlib.sha256()
    c = page.get_contents()
    if c is not None:
        h.update(c.get_data())
    res = page.get("/Resources")
    if res is not None:
        xo = res.get_object().get("/XObject")
        if xo is not None:
            for k in sorted(xo.get_object().keys()):
                o = xo.get_object()[k].get_object()
                h.update(k.encode())
                h.update(getattr(o, "_data", b"") or b"")
    return h.hexdigest()


_src_cache = {}


def source_reader(sid, fid, version):
    key = (fid, version)
    if key not in _src_cache:
        with urllib.request.urlopen(f"{BASE}/api/sessions/{sid}/files/{fid}/pdf?v={version}") as r:
            _src_cache[key] = PdfReader(io.BytesIO(r.read()))
    return _src_cache[key]


def expected(pg):
    s = st(pg)
    files = {f["cid"]: f for f in s["files"]}
    return s, [(files[p["file"]], p["page"], p["rotate"]) for p in s["seq"] if not p["excluded"] and p["file"] in files]


def verify(pg, path):
    """Output page i == expected source page i, with the right rotation."""
    s, exp = expected(pg)
    out = PdfReader(path)
    assert len(out.pages) == len(exp), f"page count {len(out.pages)} != expected {len(exp)}"
    for i, (f, idx, rot) in enumerate(exp):
        src = source_reader(s["sid"], f["id"], f["version"]).pages[idx]
        o = out.pages[i]
        assert page_sig(o) == page_sig(src), f"output page {i+1} is not {f['name']} p.{idx+1}"
        want = ((src.rotation or 0) + rot) % 360
        assert (o.rotation or 0) % 360 == want, f"page {i+1} rotation {o.rotation} != {want}"
    return [f"{f['name']}:p{idx+1}" + (f"@{rot}" if rot else "") for f, idx, rot in exp]


def markers(path):
    out = []
    for p in PdfReader(path).pages:
        t = (p.extract_text() or "")
        m = [w for w in t.split() if w.startswith("MARK-")]
        out.append(m[0] if m else "(image)")
    return out


def card_keys(pg):
    return pg.eval_on_selector_all("#page-grid .page-card", "els => els.map(e => e.dataset.key)")


def drag(pg, src_idx, dst_idx):
    cards = pg.locator("#page-grid .page-card")
    cards.nth(src_idx).drag_to(cards.nth(dst_idx), target_position={"x": 20, "y": 60})
    pg.wait_for_timeout(400)


def fresh(browser, **ctx):
    c = browser.new_context(bypass_csp=True, accept_downloads=True, **ctx)
    pg = c.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE + "/")
    pg.wait_for_function("() => typeof Sortable !== 'undefined' && typeof pdfjsLib !== 'undefined'")
    return c, pg, errs


def test(name):
    def deco(fn):
        def run(browser):
            try:
                detail = fn(browser)
                results.append((name, True, detail or ""))
                print(f"PASS  {name}  {detail or ''}")
            except Exception as e:
                results.append((name, False, f"{type(e).__name__}: {e}"))
                print(f"FAIL  {name}  {e}")
                traceback.print_exc()
        run.__name__ = fn.__name__
        return run
    return deco


# ------------------------------------------------------------------ scenarios

@test("1. Several Excel files -> one PDF (+ sheet choice, hidden sheet skipped)")
def t1(b):
    c, pg, errs = fresh(b)
    upload(pg, ["audit_wide.xlsx", "summary_narrow.xlsx", "summary_legacy.xls"])
    path, _ = download(pg, tag="t1")
    order = verify(pg, path)
    mk = markers(path)
    assert mk == ["MARK-XLSWIDE-S1", "MARK-XLSWIDE-S2", "MARK-XLSNARROW-S1", "MARK-XLSNARROW-S1"], mk
    assert "MARK-XLSWIDE-HIDDEN" not in " ".join(mk)
    # choose sheets: keep only "Notes" from the wide workbook
    to_step(pg, 1)
    row = pg.locator("#file-table-body tr").first
    row.locator("[data-act=sheets]").click()
    row.locator(".sheet-picker input[value=Audit]").uncheck()
    row.locator("[data-act=sheets-apply]").click()
    wait_idle(pg)
    path2, _ = download(pg, tag="t1b")
    verify(pg, path2)
    mk2 = markers(path2)
    assert mk2 == ["MARK-XLSWIDE-S2", "MARK-XLSNARROW-S1", "MARK-XLSNARROW-S1"], mk2
    assert not errs, errs
    c.close()
    return f"{len(mk)} pages, then {len(mk2)} after deselecting a sheet"


@test("2. Several Word documents -> one PDF")
def t2(b):
    c, pg, errs = fresh(b)
    upload(pg, ["report_a.docx", "policy_b.docx", "policy_legacy.doc"])
    path, _ = download(pg, tag="t2")
    verify(pg, path)
    mk = markers(path)
    assert mk == ["MARK-DOCXA-P1", "MARK-DOCXA-P2", "MARK-DOCXA-P3", "MARK-DOCXB-P1", "MARK-DOCXB-P2",
                  "MARK-DOCXB-P1", "MARK-DOCXB-P2"], mk
    assert not errs, errs
    c.close()
    return f"{len(mk)} pages in order"


@test("3. PDFs and images together (incl. 150-page PDF, 6000x4000 photo)")
def t3(b):
    c, pg, errs = fresh(b)
    upload(pg, ["scan_c.pdf", "photo_portrait.jpg", "chart_landscape.png", "big_150.pdf", "large_photo.jpg"])
    path, _ = download(pg, tag="t3")
    verify(pg, path)
    assert len(PdfReader(path).pages) == 4 + 1 + 1 + 150 + 1
    assert not errs, errs
    c.close()
    return "157 pages verified"


@test("4. Mixed batch: xlsx xls docx doc pdf png jpg jpeg")
def t4(b):
    c, pg, errs = fresh(b)
    upload(pg, ["audit_wide.xlsx", "summary_legacy.xls", "report_a.docx", "policy_legacy.doc", "scan_c.pdf",
                "chart_landscape.png", "photo_portrait.jpg", "phone_exif.jpeg"])
    names = pg.eval_on_selector_all("#file-table-body .status-badge.ok", "e => e.length")
    assert names == 8, f"{names} ready"
    path, sugg = download(pg, tag="t4")
    order = verify(pg, path)
    assert sugg == "MCH_Combined_Document.pdf", sugg
    assert not errs, errs
    c.close()
    return f"{len(order)} pages, default name {sugg}"


@test("5+6+8. Reorder across files (drag + move-to), rotate, remove; export matches exactly")
def t5(b):
    c, pg, errs = fresh(b)
    upload(pg, ["report_a.docx", "scan_c.pdf", "photo_portrait.jpg"])
    to_step(pg, 2)
    before = card_keys(pg)
    drag(pg, 7, 0)                       # photo (last) dragged to the front
    after = card_keys(pg)
    assert after[0] == before[7], "drag did not move the page"
    # move-to-position: the current page 2 (report p.1) -> position 6
    pg.locator("#page-grid .page-card").nth(1).locator("[data-act=move]").click()
    pg.fill("#move-input", "6")
    pg.click("#move-ok")
    pg.wait_for_timeout(300)
    keys = card_keys(pg)
    assert keys[5] == after[1], "move-to-position failed"
    # rotate page 3 right twice, page 4 left once; remove page 5
    cards = pg.locator("#page-grid .page-card")
    cards.nth(2).locator("[data-act=rotr]").click()
    pg.locator("#page-grid .page-card").nth(2).locator("[data-act=rotr]").click()
    pg.locator("#page-grid .page-card").nth(3).locator("[data-act=rotl]").click()
    pg.locator("#page-grid .page-card").nth(4).locator("[data-act=toggle]").click()
    pg.wait_for_timeout(1500)
    pg.screenshot(path=os.path.join(OUT, "t5-organize.png"), full_page=True)
    path, _ = download(pg, name="Quality Report: Oct/2026", tag="t5")
    order = verify(pg, path)
    s = st(pg)
    rots = sorted({p["rotate"] for p in s["seq"]})
    assert rots == [0, 180, 270], rots
    assert sum(p["excluded"] for p in s["seq"]) == 1
    assert len(PdfReader(path).pages) == 7
    _, sugg = path, None
    assert not errs, errs
    c.close()
    return "order: " + ", ".join(order)


@test("5b. Download uses the cleaned file name")
def t5b(b):
    c, pg, errs = fresh(b)
    upload(pg, ["scan_c.pdf"])
    _, sugg = download(pg, name='Quality Report: Oct/2026 <final>?', tag="t5b")
    assert sugg == "Quality Report Oct2026 final.pdf", sugg
    _, sugg2 = download(pg, name="تقرير الجودة", tag="t5c")
    assert sugg2 == "تقرير الجودة.pdf", sugg2
    c.close()
    return f"{sugg} / {sugg2}"


@test("7. Add more files after organizing keeps the arrangement")
def t7(b):
    c, pg, errs = fresh(b)
    upload(pg, ["scan_c.pdf", "policy_b.docx"])
    to_step(pg, 2)
    drag(pg, 5, 0)
    pg.locator("#page-grid .page-card").nth(1).locator("[data-act=rotr]").click()
    arranged = [(p["key"], p["rotate"]) for p in st(pg)["seq"]]
    pg.set_input_files("#file-input", fx("chart_landscape.png", "summary_narrow.xlsx"))
    wait_idle(pg)
    pg.wait_for_timeout(300)
    seq = st(pg)["seq"]
    assert [(p["key"], p["rotate"]) for p in seq[:len(arranged)]] == arranged, "existing arrangement changed"
    assert len(seq) == len(arranged) + 2
    assert pg.is_visible("#panel-organize"), "should stay on organize step"
    path, _ = download(pg, tag="t7")
    verify(pg, path)
    assert not errs, errs
    c.close()
    return f"{len(arranged)} arranged pages kept, 2 appended"


@test("Document reorder regroups pages (after confirmation)")
def t_docs(b):
    c, pg, errs = fresh(b)
    upload(pg, ["report_a.docx", "scan_c.pdf"])
    to_step(pg, 2)
    drag(pg, 4, 0)                     # interleave
    pg.locator("#doc-list li").nth(1).locator("[data-act=up]").click()
    pg.wait_for_selector("#confirm-dialog[open]")
    pg.click("#confirm-ok")
    pg.wait_for_timeout(300)
    path, _ = download(pg, tag="tdocs")
    mk = markers(path)
    assert mk[:4] == [f"MARK-PDFC-P{i}" for i in (2, 1, 3, 4)] or mk[:4] == [f"MARK-PDFC-P{i}" for i in range(1, 5)], mk
    assert all(m.startswith("MARK-DOCXA") for m in mk[4:]), mk
    verify(pg, path)
    c.close()
    return " ".join(mk)


@test("9. Excel wide table not clipped (last column on page, inside page bounds)")
def t9(b):
    c, pg, errs = fresh(b)
    upload(pg, ["audit_wide.xlsx"])
    path, _ = download(pg, tag="t9")
    bbox = subprocess.run(["pdftotext", "-f", "1", "-l", "1", "-bbox", path, "-"], capture_output=True, text=True).stdout
    import re
    page = re.search(r'<page width="([\d.]+)" height="([\d.]+)"', bbox)
    pw = float(page.group(1))
    words = re.findall(r'xMin="([\d.]+)" yMin="[\d.]+" xMax="([\d.]+)" yMax="[\d.]+">([^<]+)<', bbox)
    last = [w for w in words if w[2] == "LASTCOL-MARK"]
    assert last, "last column header missing from page 1"
    xmax = max(float(w[1]) for w in words)
    assert xmax < pw, f"content runs off the page ({xmax} > {pw})"
    p1 = PdfReader(path).pages[0]
    land = float(p1.mediabox.width) > float(p1.mediabox.height)
    c.close()
    return f"landscape={land}, rightmost text at {xmax:.0f}pt of {pw:.0f}pt"


@test("10. Word layout & image aspect ratios preserved")
def t10(b):
    from PIL import Image
    c, pg, errs = fresh(b)
    upload(pg, ["report_a.docx", "chart_landscape.png", "photo_portrait.jpg", "phone_exif.jpeg", "tiny.png"])
    path, _ = download(pg, tag="t10")
    verify(pg, path)
    subprocess.run(["pdftoppm", "-r", "50", "-png", path, os.path.join(OUT, "t10")], check=True)
    # expected display aspect (w/h) of the picture on each page
    want = {2: 2.0, 4: 2.0, 5: 1500 / 2250, 6: 1000 / 1600, 7: 160 / 90}
    got = {}
    for pno, ar in want.items():
        f = os.path.join(OUT, f"t10-{pno}.png")
        im = Image.open(f).convert("L")
        box = im.point(lambda v: 255 if v < 235 else 0).getbbox()
        # page 2 also has a heading; measure the coloured image area only
        if pno == 2:
            rgb = Image.open(f).convert("RGB")
            mask = Image.new("L", rgb.size)
            mask.putdata([255 if (g - r > 30) else 0 for r, g, bl in rgb.getdata()])
            box = mask.getbbox()
        w, h = box[2] - box[0], box[3] - box[1]
        got[pno] = round(w / h, 3)
        assert abs(w / h - ar) / ar < 0.06, f"page {pno} aspect {w/h:.3f} != {ar:.3f}"
    txt = subprocess.run(["pdftotext", "-f", "1", "-l", "1", path, "-"], capture_output=True, text=True).stdout
    assert "Cell 3,2" in txt, "Word table text missing"
    c.close()
    return "aspect ok " + json.dumps(got)


@test("11. Invalid, protected, empty, oversized files give clear errors; good files still work")
def t11(b):
    c, pg, errs = fresh(b)
    big = os.path.join(OUT, "too_big.pdf")
    with open(os.path.join(FX, "scan_c.pdf"), "rb") as f:
        data = f.read()
    with open(big, "wb") as f:
        f.write(data + b"\n%" + b"0" * (52 * 1024 * 1024))
    bad = ["notes.txt", "not_really.docx", "locked.pdf", "locked.docx", "broken.pdf", "empty.pdf", "fake_image.png"]
    pg.set_input_files("#file-input", fx(*bad) + [big, os.path.join(FX, "scan_c.pdf")])
    wait_idle(pg)
    rows = pg.eval_on_selector_all("#file-table-body tr", """rs => rs.map(r => [
        r.querySelector('.file-name').textContent,
        r.querySelector('.status-badge').textContent.trim(),
        (r.querySelector('.status-detail')||{}).textContent || ''])""")
    msgs = {n: (s, d) for n, s, d in rows}
    for n in bad + ["too_big.pdf"]:
        assert msgs[n][0] == "Not added", (n, msgs[n])
        assert len(msgs[n][1]) > 10, (n, msgs[n])
    assert "password" in msgs["locked.pdf"][1] and "password" in msgs["locked.docx"][1]
    assert "50 MB" in msgs["too_big.pdf"][1]
    assert msgs["scan_c.pdf"][0] == "Ready"
    path, _ = download(pg, tag="t11")
    verify(pg, path)
    c.close()
    return "; ".join(f"{n}: {d}" for n, (s, d) in msgs.items() if s != "Ready")


@test("Start over asks first, clears the screen and deletes server files")
def t_reset(b):
    c, pg, errs = fresh(b)
    upload(pg, ["scan_c.pdf"])
    sid = st(pg)["sid"]
    to_step(pg, 3)
    pg.click("#btn-start-over")
    pg.wait_for_selector("#confirm-dialog[open]")
    pg.click("#confirm-cancel")
    assert pg.is_visible("#panel-export"), "cancel should keep work"
    pg.click("#btn-start-over")
    pg.click("#confirm-ok")
    pg.wait_for_selector("#panel-upload:not([hidden])")
    pg.wait_for_timeout(500)
    assert pg.eval_on_selector_all("#file-table-body tr", "e => e.length") == 0
    code = None
    try:
        urllib.request.urlopen(f"{BASE}/api/sessions/{sid}")
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 404, f"session still on server ({code})"
    c.close()
    return "session deleted on server"


@test("Refresh keeps the arrangement")
def t_refresh(b):
    c, pg, errs = fresh(b)
    upload(pg, ["scan_c.pdf", "photo_portrait.jpg"])
    to_step(pg, 2)
    drag(pg, 4, 0)
    pg.locator("#page-grid .page-card").nth(0).locator("[data-act=rotr]").click()
    before = st(pg)["seq"]
    pg.reload()
    pg.wait_for_selector("#panel-organize:not([hidden])", timeout=15000)
    after = st(pg)["seq"]
    assert before == after
    c.close()
    return "arrangement restored after reload"


@test("Server restart mid-work: clear message, screen resets, new files work")
def t_restart(b):
    c, pg, errs = fresh(b)
    upload(pg, ["scan_c.pdf", "photo_portrait.jpg"])
    to_step(pg, 2)
    sid = st(pg)["sid"]
    # A Render restart/redeploy starts with an empty disk: the session no longer exists
    req = urllib.request.Request(f"{BASE}/api/sessions/{sid}", method="DELETE")
    urllib.request.urlopen(req).read()
    pg.click("#btn-to-export")
    pg.wait_for_selector("#error-banner:not([hidden])", timeout=20000)
    msg = pg.inner_text("#error-message")
    assert "expired" in msg.lower(), msg
    assert pg.is_visible("#panel-upload"), "should return to step 1"
    assert pg.eval_on_selector_all("#file-table-body tr", "e => e.length") == 0
    upload(pg, ["scan_c.pdf"])
    assert st(pg)["sid"] != sid, "a new session should be created"
    path, _ = download(pg, tag="trestart")
    verify(pg, path)
    assert not errs, errs
    c.close()
    return msg[:80] + "..."


@test("12a. Desktop layout (1280px) - no horizontal overflow")
def t12a(b):
    c, pg, errs = fresh(b, viewport={"width": 1280, "height": 900})
    upload(pg, ["audit_wide.xlsx", "report_a.docx", "chart_landscape.png", "locked.pdf"])
    for n in (1, 2, 3):
        to_step(pg, n)
        pg.wait_for_timeout(1500)
        ov = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert ov <= 0, f"step {n} overflows by {ov}px"
        pg.screenshot(path=os.path.join(OUT, f"desktop-step{n}.png"), full_page=True)
    c.close()
    return "3 steps, screenshots saved"


@test("12b. Mobile (iPhone 13 emulation, touch) - layout + touch drag + export")
def t12b(b, ):
    dev = dict(b._playwright.devices["iPhone 13"])
    dev.pop("default_browser_type", None)
    c, pg, errs = fresh(b, **dev)
    upload(pg, ["report_a.docx", "scan_c.pdf", "phone_exif.jpeg", "locked.pdf"])
    for n in (1, 2):
        to_step(pg, n)
        pg.wait_for_timeout(1500)
        ov = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert ov <= 0, f"step {n} overflows by {ov}px"
        pg.screenshot(path=os.path.join(OUT, f"mobile-step{n}.png"), full_page=True)
    # touch: press-and-hold then drag (Sortable's touch path); cards must be on screen
    pg.evaluate("document.getElementById('page-grid').scrollIntoView({block: 'start'})")
    pg.wait_for_timeout(300)
    cards = pg.locator("#page-grid .page-card")
    src, dst = cards.nth(0).bounding_box(), cards.nth(3).bounding_box()
    before = card_keys(pg)
    cdp = pg.context.new_cdp_session(pg)
    sx, sy = src["x"] + src["width"] / 2, src["y"] + 40
    tx, ty = dst["x"] + dst["width"] / 2 + 10, dst["y"] + 60
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": sx, "y": sy}]})
    pg.wait_for_timeout(400)
    steps = 12
    for i in range(1, steps + 1):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [
            {"x": sx + (tx - sx) * i / steps, "y": sy + (ty - sy) * i / steps}]})
        pg.wait_for_timeout(30)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    pg.wait_for_timeout(500)
    after = card_keys(pg)
    moved = after != before
    # tap buttons
    pg.locator("#page-grid .page-card").nth(1).locator("[data-act=rotr]").tap()
    pg.locator("#page-grid .page-card").nth(2).locator("[data-act=toggle]").tap()
    to_step(pg, 3)
    pg.wait_for_timeout(1500)
    ov = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert ov <= 0, f"step 3 overflows by {ov}px"
    pg.screenshot(path=os.path.join(OUT, "mobile-step3.png"), full_page=False)
    path, _ = download(pg, tag="t12b")
    verify(pg, path)
    assert not errs, errs
    c.close()
    assert moved, "touch drag didn't reorder pages"
    return "touch drag reordered pages; export verified"


if __name__ == "__main__":
    only = sys.argv[1:]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        browser._playwright = p
        for t in [t1, t2, t3, t4, t5, t5b, t7, t_docs, t9, t10, t11, t_reset, t_refresh, t_restart, t12a, t12b]:
            if only and t.__name__ not in only:
                continue
            _src_cache.clear()
            t(browser)
        browser.close()
    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    with open(os.path.join(OUT, "results.json"), "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    sys.exit(1 if failed else 0)
