"""
Browser test for the file-type colour system.   BASE=http://127.0.0.1:5052 FX=tests/fixtures python tests/test_file_colors.py

Checks, in the real page, that every file type gets its exact colour on the indicator dot,
a tinted badge with readable text, the same colour on the Documents bar and page-card edge,
that colours follow the file (not its position) through reordering and removal, and that
Ready / Not added status styling is unchanged and independent of the file type.
"""
import os
import shutil
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:5052")
FX = os.environ.get("FX", os.path.join(os.path.dirname(__file__), "fixtures"))
OUT = os.environ.get("OUT", tempfile.gettempdir())

HEX = {"excel": "#217346", "word": "#2B579A", "pdf": "#D32F2F", "sheets": "#0F9D58",
       "image": "#4285F4", "ppt": "#E37400", "other": "#64748B"}
TINT = {"excel": "#E9F1EC", "word": "#EAEEF5", "pdf": "#FBEAEA", "sheets": "#E7F5EE",
        "image": "#ECF3FE", "ppt": "#FCF1E6", "other": "#F0F1F3"}
INK = {"excel": "#217346", "word": "#2B579A", "pdf": "#BE2A2A", "sheets": "#0B7441",
       "image": "#3265B9", "ppt": "#9F5100", "other": "#58667A"}
STATUS = {"Ready": ("rgb(230, 247, 243)", "rgb(11, 122, 100)"),       # --success-bg / --success-ink
          "Not added": ("rgb(253, 236, 234)", "rgb(180, 35, 24)")}     # --error-bg / --error

# name in upload -> (source fixture, expected type, expected badge, expected status)
FILES = {
    "Ward Report.DOCX":      ("report_a.docx", "word", "WORD", "Ready"),
    "Audit 2026.XLSX":       ("audit_wide.xlsx", "excel", "EXCEL", "Ready"),
    "legacy.xls":            ("summary_legacy.xls", "excel", "EXCEL", "Ready"),
    "Scan.PDF":              ("scan_c.pdf", "pdf", "PDF", "Ready"),
    "chart.png":             ("chart_landscape.png", "image", "IMAGE", "Ready"),
    "Photo.JPG":             ("photo_portrait.jpg", "image", "IMAGE", "Ready"),
    "phone.jpeg":            ("phone_exif.jpeg", "image", "IMAGE", "Ready"),
    "Locked.pdf":            ("locked.pdf", "pdf", "PDF", "Not added"),       # failed conversion stays red
    "Slides.PPTX":           ("notes.txt", "ppt", "PPT", "Not added"),
    "anim.gif":              ("notes.txt", "image", "IMAGE", "Not added"),
    "Budget.gsheet":         ("notes.txt", "sheets", "SHEETS", "Not added"),
    "notes.txt":             ("notes.txt", "other", "TXT", "Not added"),
}

failures = []


def rgb(h):
    h = h.lstrip("#")
    return "rgb(%d, %d, %d)" % tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def check(cond, label):
    print(("  ok    " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


def rows(pg):
    return pg.eval_on_selector_all("#file-table-body tr", """trs => trs.map(tr => {
        const cs = el => el ? getComputedStyle(el) : null;
        const dot = cs(tr.querySelector('.doc-swatch')), tag = cs(tr.querySelector('.fmt-tag'));
        const st = tr.querySelector('.status-badge');
        return { name: tr.querySelector('.file-name').textContent, cls: tr.className,
                 dot: dot.backgroundColor, tagBg: tag.backgroundColor, tagInk: tag.color,
                 tagBorder: tag.borderTopColor, tagText: tr.querySelector('.fmt-tag').textContent,
                 tagPad: tag.padding, tagFont: tag.fontSize + ' ' + tag.fontWeight, tagRadius: tag.borderRadius,
                 status: st.textContent.trim(), stBg: cs(st).backgroundColor, stInk: cs(st).color };
    })""")


def verify_rows(pg, label):
    print(label)
    for r in rows(pg):
        _, t, badge, status = FILES[r["name"]]
        ok = (r["cls"] == f"ft-{t}" and r["dot"] == rgb(HEX[t]) and r["tagBg"] == rgb(TINT[t])
              and r["tagInk"] == rgb(INK[t]) and r["tagText"] == badge)
        check(ok, f"{r['name']:18s} {t:6s} dot {r['dot']:18s} badge {r['tagText']:6s} bg {r['tagBg']} ink {r['tagInk']}")
        want_bg, want_ink = STATUS[status]
        st_ok = r["status"].startswith(status) and r["stBg"] == want_bg and r["stInk"] == want_ink
        check(st_ok, f"{'':18s} status '{r['status'][:9]}' keeps its own colours ({r['stBg']}, {r['stInk']})")
        check(r["tagPad"] == "1px 6px" and r["tagFont"] == "10.5px 700" and r["tagRadius"] == "4px",
              f"{'':18s} badge shape/size/typography unchanged")


def main():
    tmp = tempfile.mkdtemp(prefix="ftcolors-")
    paths = []
    for name, (src, *_rest) in FILES.items():
        dst = os.path.join(tmp, name)
        shutil.copyfile(os.path.join(FX, src), dst)
        paths.append(dst)

    with sync_playwright() as p:
        b = p.chromium.launch()
        # mobile first: the desktop pass removes files from FILES while testing removal
        for label, ctx_kw in (("mobile", {k: v for k, v in p.devices["iPhone 13"].items() if k != "default_browser_type"}),
                              ("desktop", {"viewport": {"width": 1280, "height": 900}})):
            ctx = b.new_context(bypass_csp=True, **ctx_kw)
            pg = ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(BASE + "/")
            pg.set_input_files("#file-input", paths)
            pg.wait_for_timeout(300)
            pg.wait_for_function("() => !document.querySelector('.status-badge.pending')", timeout=180000)
            verify_rows(pg, f"[{label}] after upload:")
            pg.locator("#file-block").screenshot(path=os.path.join(OUT, f"colors-{label}-table.png"))

            if label == "desktop":
                # reorder: move row 3 to the top, and row 1 down
                pg.locator("#file-table-body tr").nth(2).locator("[data-act=up]").click()
                pg.locator("#file-table-body tr").nth(1).locator("[data-act=up]").click()
                pg.locator("#file-table-body tr").nth(1).locator("[data-act=down]").click()
                verify_rows(pg, "[desktop] after reordering:")
                # remove a Word row and an error row
                for n in ("Ward Report.DOCX", "notes.txt"):
                    pg.locator("#file-table-body tr", has_text=n).locator("[data-act=remove]").click()
                    pg.wait_for_timeout(200)
                    FILES.pop(n)
                verify_rows(pg, "[desktop] after removing two files:")

                # step 2: documents list + page cards use the same type colours
                pg.click("#step-btn-2")
                pg.wait_for_timeout(1500)
                docs = pg.eval_on_selector_all("#doc-list li", "ls => ls.map(l => [l.querySelector('.doc-name').textContent, getComputedStyle(l.querySelector('.doc-bar')).backgroundColor])")
                print("[desktop] organize step:")
                for name, col in docs:
                    t = FILES[name][1]
                    check(col == rgb(HEX[t]), f"document bar  {name:18s} {col}")
                cards = pg.eval_on_selector_all("#page-grid .page-card", "cs => cs.map(c => [c.querySelector('.page-src').textContent, getComputedStyle(c).borderTopColor])")
                bad = [c for c in cards if c[1] != rgb(HEX[FILES[c[0]][1]])]
                check(not bad, f"page-card edges: {len(cards)} cards, all in their file-type colour {bad[:2]}")
                pg.screenshot(path=os.path.join(OUT, "colors-desktop-organize.png"), full_page=True)
            check(not errs, f"[{label}] no page errors {errs[:1]}")
            ctx.close()
        b.close()
    print()
    print("ALL COLOUR CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
