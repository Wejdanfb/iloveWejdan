"""
Split-deployment test: the page comes from one site (Vercel) and every file goes to the
API on another (Render). Runs WITHOUT bypassing the page's Content-Security-Policy, so it
also proves the browser is allowed to talk to the backend.

  BASE=https://ilove-wejdan.vercel.app API=https://ilovewejdan.onrender.com \
      FX=tests/fixtures python tests/test_split.py

Uploads Word (.docx, .doc), Excel (.xlsx, .xls), PDF, PNG, JPG and JPEG through the real
page, checks each is converted, builds the combined PDF, downloads it and verifies every
page by its text marker / source.
"""
import io
import json
import os
import sys
import tempfile
import urllib.request

from playwright.sync_api import sync_playwright
from pypdf import PdfReader

BASE = os.environ["BASE"].rstrip("/")
API = os.environ["API"].rstrip("/")
FX = os.environ.get("FX", os.path.join(os.path.dirname(__file__), "fixtures"))
OUT = os.environ.get("OUT", tempfile.gettempdir())
FILES = ["report_a.docx", "policy_legacy.doc", "audit_wide.xlsx", "summary_legacy.xls",
         "scan_c.pdf", "chart_landscape.png", "photo_portrait.jpg", "phone_exif.jpeg"]
EXPECT_PAGES = {"report_a.docx": 3, "policy_legacy.doc": 2, "audit_wide.xlsx": 2, "summary_legacy.xls": 1,
                "scan_c.pdf": 4, "chart_landscape.png": 1, "photo_portrait.jpg": 1, "phone_exif.jpeg": 1}
failures = []


def check(cond, label):
    print(("  ok    " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        for label, kw in (("desktop", {"viewport": {"width": 1280, "height": 900}}),
                          ("iPhone", {k: v for k, v in p.devices["iPhone 13"].items() if k != "default_browser_type"})):
            print(f"[{label}] page {BASE}  ->  API {API}")
            ctx = b.new_context(accept_downloads=True, **kw)           # CSP enforced
            pg = ctx.new_page()
            errs, csp, api_calls = [], [], set()
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: m.type == "error" and ("Content Security Policy" in m.text or "CORS" in m.text) and csp.append(m.text))
            pg.on("request", lambda r: "/api/" in r.url and api_calls.add(r.url.split("/api/")[0]))
            pg.goto(BASE + "/", wait_until="networkidle")
            check(pg.title() == "I Love Wejdan", f"page loads: title '{pg.title()}'")
            check(pg.get_attribute("body", "data-api-base") in (API, ""), f"page points at the API: '{pg.get_attribute('body', 'data-api-base')}'")

            pg.set_input_files("#file-input", [os.path.join(FX, f) for f in FILES])
            pg.locator("#file-table-body .status-badge.pending").first.wait_for(state="detached", timeout=300000) \
                if pg.locator("#file-table-body .status-badge.pending").count() else None
            pg.wait_for_selector("#file-table-body tr:nth-child(%d) .status-badge:not(.pending)" % len(FILES), timeout=300000)
            for _ in range(600):
                if pg.locator("#file-table-body .status-badge.pending").count() == 0:
                    break
                pg.wait_for_timeout(500)
            rows = pg.locator("#file-table-body tr")
            for i in range(rows.count()):
                r = rows.nth(i)
                name = r.locator(".file-name").inner_text()
                status = r.locator(".status-badge").inner_text().strip()
                pages = r.locator("td.col-count").inner_text().strip().replace("Pages: ", "")
                detail = r.locator(".status-detail").inner_text() if r.locator(".status-detail").count() else ""
                check(status.startswith("Ready") and pages == str(EXPECT_PAGES[name]),
                      f"{name:20s} {status:8s} pages={pages} {detail}")

            pg.click("#step-btn-2")
            pg.locator("#page-grid").scroll_into_view_if_needed()      # thumbnails draw when visible
            pg.wait_for_selector("#page-grid .page-card canvas", timeout=60000)
            pg.click("#step-btn-3")
            pg.wait_for_selector("#export-ready:not([hidden])", timeout=180000)
            pg.locator("#preview").scroll_into_view_if_needed()
            pg.wait_for_selector("#preview canvas", timeout=60000)
            total = int(pg.inner_text("#out-pages"))
            check(total == sum(EXPECT_PAGES.values()), f"combined PDF has {total} pages, preview rendered")
            with pg.expect_download(timeout=120000) as d:
                pg.click("#btn-download")
            path = os.path.join(OUT, f"split-{label}.pdf")
            d.value.save_as(path)
            reader = PdfReader(path)
            marks = []
            for page in reader.pages:
                t = page.extract_text() or ""
                m = [w for w in t.split() if w.startswith("MARK-")]
                marks.append(m[0] if m else "(image)")
            want = ["MARK-DOCXA-P1", "MARK-DOCXA-P2", "MARK-DOCXA-P3", "MARK-DOCXB-P1", "MARK-DOCXB-P2",
                    "MARK-XLSWIDE-S1", "MARK-XLSWIDE-S2", "MARK-XLSNARROW-S1",
                    "MARK-PDFC-P1", "MARK-PDFC-P2", "MARK-PDFC-P3", "MARK-PDFC-P4", "(image)", "(image)", "(image)"]
            check(marks == want, f"downloaded '{d.value.suggested_filename}': {len(reader.pages)} pages, Word+Excel+PDF+images in order")
            check(api_calls == {API} or (API == BASE and api_calls <= {BASE, ""}), f"all API traffic went to {sorted(api_calls)}")
            check(not csp, f"no CSP/CORS errors {csp[:1]}")
            check(not errs, f"no page errors {errs[:1]}")
            pg.screenshot(path=os.path.join(OUT, f"split-{label}.png"))
            ctx.close()
        b.close()
    print("\nALL SPLIT-DEPLOYMENT CHECKS PASSED" if not failures else f"\n{len(failures)} CHECK(S) FAILED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
