"""
MCH PDF Organizer - document conversion engine.

Turns one uploaded file into one PDF. No Flask code here: the web layer (app.py)
only stores files and calls convert().

  PDF          -> checked and kept byte-for-byte (pages are never re-rendered)
  PNG/JPG/JPEG -> img2pdf (JPEG data is embedded as-is, PNG losslessly)
  DOCX/DOC     -> LibreOffice headless, images kept at full resolution
  XLSX/XLS     -> print setup adjusted for fit-to-width (and landscape when the
                  table is wide), chosen sheets only, then LibreOffice headless

Privacy: nothing in this module logs file names or document content.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field

import img2pdf
from lxml import etree
from PIL import Image, ImageOps
from pypdf import PdfReader

log = logging.getLogger("mch.convert")

# Upload extension -> kind
KINDS = {
    ".pdf": "pdf",
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".docx": "word", ".doc": "word",
    ".xlsx": "excel", ".xls": "excel",
}
SUPPORTED_EXTENSIONS = tuple(KINDS)

SOFFICE_TIMEOUT = int(os.environ.get("SOFFICE_TIMEOUT", "240"))
_soffice_lock = threading.Lock()          # one LibreOffice process at a time (memory)

ZIP_MAGIC = b"PK\x03\x04"
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"    # legacy .doc/.xls and encrypted OOXML

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


class ConversionError(Exception):
    """A user-facing conversion failure. `code` is stable, `message` is shown in the UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Result:
    pdf_path: str
    pages: int
    page_sizes: list = field(default_factory=list)   # [(w_pt, h_pt)] as displayed
    sheets: list | None = None                        # Excel only
    notes: list = field(default_factory=list)          # non-blocking warnings


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------

def kind_for(filename: str) -> str | None:
    return KINDS.get(os.path.splitext(filename.lower())[1])


def convert(src: str, ext: str, workdir: str, *, sheets: list[str] | None = None,
            image_mode: str = "a4") -> Result:
    """Convert `src` (an uploaded file with extension `ext`) to <workdir>/converted.pdf."""
    ext = ext.lower()
    kind = KINDS.get(ext)
    if kind is None:
        raise ConversionError("unsupported", "This file type isn't supported.")
    if os.path.getsize(src) == 0:
        raise ConversionError("empty", "The file is empty (0 bytes).")

    final = os.path.join(workdir, "converted.pdf")
    out = os.path.join(workdir, "converted.tmp.pdf")      # never expose a half-written file
    t0 = time.monotonic()
    try:
        if kind == "pdf":
            res = _from_pdf(src, out)
        elif kind == "image":
            res = _from_image(src, out, image_mode)
        elif kind == "word":
            res = _from_word(src, ext, out, workdir)
        else:
            res = _from_excel(src, ext, out, workdir, sheets)
        os.replace(out, final)
        res.pdf_path = final
    finally:
        if os.path.exists(out):
            os.remove(out)
    log.info("converted kind=%s pages=%d ms=%d", kind, res.pages, (time.monotonic() - t0) * 1000)
    return res


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

def inspect_pdf(path: str) -> tuple[int, list]:
    """Open a PDF the way the merger will; return (page count, displayed sizes)."""
    try:
        reader = PdfReader(path, strict=False)
    except Exception:
        raise ConversionError("corrupt", "The PDF couldn't be opened - it may be damaged.")
    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")      # owner-password-only PDFs still open
        except Exception:
            ok = 0
        if not ok:
            raise ConversionError("password", "This PDF is password-protected. Remove the password and upload it again.")
    try:
        sizes = []
        for p in reader.pages:
            w, h = float(p.mediabox.width), float(p.mediabox.height)
            if (p.rotation or 0) % 180:
                w, h = h, w
            sizes.append((round(w, 1), round(h, 1)))
    except Exception:
        raise ConversionError("corrupt", "The PDF's pages couldn't be read - it may be damaged.")
    if not sizes:
        raise ConversionError("no_pages", "The PDF has no pages.")
    return len(sizes), sizes


def _from_pdf(src: str, out: str) -> Result:
    with open(src, "rb") as f:
        head = f.read(1024)
    if b"%PDF" not in head:
        raise ConversionError("corrupt", "This isn't a valid PDF file.")
    n, sizes = inspect_pdf(src)
    shutil.copyfile(src, out)            # unchanged: original quality and appearance
    return Result(out, n, sizes)


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

A4 = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
IMAGE_MARGIN = img2pdf.mm_to_pt(10)


def _from_image(src: str, out: str, mode: str) -> Result:
    try:
        with Image.open(src) as im:
            im.verify()
        im = Image.open(src)
        im.load()
    except Exception:
        raise ConversionError("corrupt", "The image couldn't be opened - it may be damaged or not really an image.")

    fmt = (im.format or "").upper()
    if fmt not in ("PNG", "JPEG", "MPO"):
        raise ConversionError("corrupt", f"The file content is {fmt or 'unknown'}, not PNG or JPEG.")

    # Width/height as the photo should be seen (EXIF orientation applied)
    raw_size = im.size
    upright = ImageOps.exif_transpose(im)
    w, h = upright.size

    data_path = src
    rotation = img2pdf.Rotation.ifvalid
    # img2pdf embeds JPEG and plain PNG streams untouched. Anything it can't embed
    # losslessly (transparency, 16-bit, CMYK PNG, multi-frame MPO) is flattened to an
    # 8-bit RGB PNG first - still lossless for what is visible.
    needs_flatten = (
        fmt == "MPO"
        or im.mode in ("RGBA", "LA", "PA", "I;16", "I;16B", "I", "F")
        or (im.mode == "P" and "transparency" in im.info)
        or (fmt == "PNG" and im.mode == "CMYK")
    )
    if needs_flatten:
        rgb = upright.convert("RGBA")
        bg = Image.new("RGB", rgb.size, (255, 255, 255))
        bg.paste(rgb, mask=rgb.getchannel("A"))
        data_path = os.path.join(os.path.dirname(out), "_flat.png")
        bg.save(data_path, "PNG", optimize=False)
        rotation = img2pdf.Rotation.none          # already upright
    im.close()

    if mode == "original":
        layout = img2pdf.default_layout_fun
    else:
        # img2pdf lays the page out for the image as stored and then applies the EXIF
        # rotation with /Rotate, so the page must match the *stored* shape in that case.
        if rotation == img2pdf.Rotation.ifvalid:
            w, h = raw_size
        portrait = h >= w
        page = A4 if portrait else (A4[1], A4[0])
        layout = img2pdf.get_layout_fun(
            pagesize=page, border=(IMAGE_MARGIN, IMAGE_MARGIN),
            fit=img2pdf.FitMode.shrink, auto_orient=False)

    try:
        with open(out, "wb") as f:
            f.write(img2pdf.convert(data_path, layout_fun=layout, rotation=rotation))
    except Exception:
        raise ConversionError("failed", "The image couldn't be converted to a PDF page.")
    finally:
        if data_path != src and os.path.exists(data_path):
            os.remove(data_path)

    n, sizes = inspect_pdf(out)
    return Result(out, n, sizes)


# --------------------------------------------------------------------------
# Office: encryption / format checks
# --------------------------------------------------------------------------

def _office_precheck(src: str, ext: str) -> str:
    """Return the real container ('zip' or 'cfb'); raise for encrypted or bogus files."""
    with open(src, "rb") as f:
        head = f.read(8)
    if head.startswith(ZIP_MAGIC):
        try:
            with zipfile.ZipFile(src) as z:
                bad = z.testzip()
                names = set(z.namelist())
        except Exception:
            raise ConversionError("corrupt", "The file couldn't be opened - it may be damaged.")
        if bad is not None or "[Content_Types].xml" not in names:
            raise ConversionError("corrupt", "The file is damaged or isn't a real Office document.")
        return "zip"
    if head == CFB_MAGIC:
        try:
            import msoffcrypto
            with open(src, "rb") as f:
                if msoffcrypto.OfficeFile(f).is_encrypted():
                    raise ConversionError("password",
                                          "This file is password-protected. Open it, remove the password, save and upload again.")
        except ConversionError:
            raise
        except Exception:
            pass        # not something msoffcrypto understands; LibreOffice decides
        if ext in (".docx", ".xlsx"):
            # An OLE container with a .docx/.xlsx name is either encrypted OOXML
            # (caught above) or a legacy file with the wrong extension - both fine for LO.
            pass
        return "cfb"
    if ext in (".doc", ".xls"):
        # LibreOffice also opens RTF/HTML saved with a .doc/.xls name; let it try.
        return "other"
    raise ConversionError("corrupt", "The file is damaged or isn't a real Office document.")


# --------------------------------------------------------------------------
# LibreOffice
# --------------------------------------------------------------------------

def _soffice_bin() -> str:
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for p in (r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if os.path.exists(p):
            return p
    raise ConversionError("no_converter",
                          "The Office converter (LibreOffice) isn't installed on the server.")


def _profile_url() -> str:
    d = os.path.join(tempfile.gettempdir(), f"mch-lo-profile-{os.getpid()}")
    os.makedirs(d, exist_ok=True)
    return "file:///" + d.replace("\\", "/").lstrip("/")


def soffice_convert(src: str, target: str, outdir: str, _retry: bool = False) -> str:
    """Run `soffice --convert-to target src`; return the produced file path."""
    cmd = [_soffice_bin(), "--headless", "--invisible", "--norestore", "--nolockcheck",
           "--nodefault", "--nofirststartwizard", f"-env:UserInstallation={_profile_url()}",
           "--convert-to", target, "--outdir", outdir, src]
    stem = os.path.splitext(os.path.basename(src))[0]
    ext = target.split(":")[0]
    produced = os.path.join(outdir, f"{stem}.{ext}")
    if os.path.exists(produced):
        os.remove(produced)
    with _soffice_lock:
        kwargs = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, **kwargs)
        try:
            proc.communicate(timeout=SOFFICE_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise ConversionError("timeout",
                                  "Conversion took too long and was stopped. The file may be very large or damaged.")
    if proc.returncode != 0 or not os.path.exists(produced) or os.path.getsize(produced) == 0:
        if ":{" in target and not _retry:
            # Older LibreOffice builds don't understand JSON export options -> plain defaults
            log.warning("soffice failed with export options rc=%s target=%s; retrying with defaults",
                        proc.returncode, ext)
            return soffice_convert(src, ext, outdir, _retry=True)
        log.warning("soffice failed rc=%s target=%s", proc.returncode, ext)
        raise ConversionError("failed",
                              "The file couldn't be converted. It may be damaged, password-protected or in an unusual format.")
    return produced


def _kill(proc):
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        pass
    try:
        proc.communicate(timeout=10)
    except Exception:
        pass


def _pdf_filter(module: str) -> str:
    # Keep embedded images at full resolution with high JPEG quality.
    opts = {
        "ReduceImageResolution": {"type": "boolean", "value": "false"},
        "Quality": {"type": "long", "value": "95"},
        "ExportFormFields": {"type": "boolean", "value": "false"},
    }
    return f"pdf:{module}:{json.dumps(opts, separators=(',', ':'))}"


# --------------------------------------------------------------------------
# Word
# --------------------------------------------------------------------------

def _from_word(src: str, ext: str, out: str, workdir: str) -> Result:
    _office_precheck(src, ext)
    stage = os.path.join(workdir, "stage")
    os.makedirs(stage, exist_ok=True)
    staged = os.path.join(stage, "document" + ext)
    shutil.copyfile(src, staged)
    try:
        pdf = soffice_convert(staged, _pdf_filter("writer_pdf_Export"), stage)
        shutil.move(pdf, out)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    n, sizes = inspect_pdf(out)
    return Result(out, n, sizes)


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------

def _from_excel(src: str, ext: str, out: str, workdir: str, selected: list[str] | None) -> Result:
    container = _office_precheck(src, ext)
    stage = os.path.join(workdir, "stage")
    os.makedirs(stage, exist_ok=True)
    try:
        xlsx = os.path.join(stage, "book.xlsx")
        if container == "zip":
            shutil.copyfile(src, xlsx)
        else:
            # Legacy .xls: LibreOffice re-saves it as .xlsx so the same print
            # preparation applies.
            staged = os.path.join(stage, "legacy" + ext)
            shutil.copyfile(src, staged)
            produced = soffice_convert(staged, "xlsx:Calc MS Excel 2007 XML", stage)
            shutil.move(produced, xlsx)
            os.remove(staged)

        prepared = os.path.join(stage, "print.xlsx")
        sheets, notes = prepare_workbook(xlsx, prepared, selected)
        pdf = soffice_convert(prepared, _pdf_filter("calc_pdf_Export"), stage)
        shutil.move(pdf, out)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    try:
        n, sizes = inspect_pdf(out)
    except ConversionError as e:
        if e.code == "no_pages":
            raise ConversionError("no_pages", "The selected sheets have nothing to print.")
        raise
    return Result(out, n, sizes, sheets=sheets, notes=notes)


def _qn(tag: str) -> str:
    return f"{{{NS_MAIN}}}{tag}"


# Excel paperSize codes -> (width, height) in points
_PAPER = {1: (612, 792), 5: (612, 1008), 8: (842, 1191), 9: (595, 842), 11: (420, 595)}
# Order of CT_Worksheet children after which <pageSetup> must come
_BEFORE_PAGESETUP = ("sheetPr", "dimension", "sheetViews", "sheetFormatPr", "cols", "sheetData",
                     "sheetCalcPr", "sheetProtection", "protectedRanges", "scenarios", "autoFilter",
                     "sortState", "dataConsolidate", "customSheetViews", "mergeCells", "phoneticPr",
                     "conditionalFormatting", "dataValidations", "hyperlinks", "printOptions",
                     "pageMargins")
_SHEETPR_BEFORE = ("tabColor", "outlinePr")   # <pageSetUpPr> comes after these


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _used_columns(root) -> tuple[int, int]:
    """First and last column that actually hold a value (scan, not <dimension>)."""
    lo, hi = None, None
    for c in root.iter(_qn("c")):
        has_value = c.find(_qn("v")) is not None or c.find(_qn("is")) is not None
        if not has_value:
            continue
        m = re.match(r"([A-Z]+)", c.get("r", ""))
        if not m:
            continue
        i = _col_index(m.group(1))
        lo = i if lo is None else min(lo, i)
        hi = i if hi is None else max(hi, i)
    return (lo or 1, hi or 1)


def _content_width_pt(root, first: int, last: int) -> float:
    fmt = root.find(_qn("sheetFormatPr"))
    default_chars = 8.43
    if fmt is not None:
        if fmt.get("defaultColWidth"):
            default_chars = float(fmt.get("defaultColWidth"))
        elif fmt.get("baseColWidth"):
            default_chars = float(fmt.get("baseColWidth")) + 0.71
    widths = {}
    cols = root.find(_qn("cols"))
    if cols is not None:
        for col in cols.findall(_qn("col")):
            try:
                a, b = int(col.get("min")), int(col.get("max"))
            except (TypeError, ValueError):
                continue
            hidden = col.get("hidden") in ("1", "true")
            w = 0.0 if hidden else float(col.get("width") or default_chars)
            for i in range(max(a, first), min(b, last) + 1):
                widths[i] = w
    total_chars = sum(widths.get(i, default_chars) for i in range(first, last + 1))
    # Excel width unit ~ 7px per char + 5px padding at 96dpi -> points
    return total_chars * 7 * 0.75 + (last - first + 1) * 5 * 0.75


def prepare_workbook(src: str, dst: str, selected: list[str] | None):
    """Copy `src` to `dst` changing only print settings and sheet visibility.

    Returns (sheets, notes). sheets = [{name, hidden_in_file, selected, kind}]
    """
    notes = []
    with zipfile.ZipFile(src) as zin:
        names = zin.namelist()
        try:
            wb = etree.fromstring(zin.read("xl/workbook.xml"))
            rels = etree.fromstring(zin.read("xl/_rels/workbook.xml.rels"))
        except KeyError:
            raise ConversionError("corrupt", "The workbook structure is damaged (no sheet list found).")
        except etree.XMLSyntaxError:
            raise ConversionError("corrupt", "The workbook structure is damaged.")

        targets = {}
        for r in rels.findall(f"{{{NS_PKG_REL}}}Relationship"):
            t = r.get("Target", "")
            t = t.lstrip("/") if t.startswith("/") else "xl/" + t
            targets[r.get("Id")] = (os.path.normpath(t).replace("\\", "/"), r.get("Type", ""))

        sheet_els = wb.findall(f"{_qn('sheets')}/{_qn('sheet')}")
        if not sheet_els:
            raise ConversionError("no_pages", "The workbook has no sheets.")

        info = []
        for idx, s in enumerate(sheet_els):
            rid = s.get(f"{{{NS_REL}}}id")
            path, rtype = targets.get(rid, (None, ""))
            hidden = s.get("state") in ("hidden", "veryHidden")
            kind = "chart" if rtype.endswith("/chartsheet") else "sheet"
            info.append({"name": s.get("name"), "hidden_in_file": hidden, "kind": kind,
                         "_el": s, "_path": path, "_idx": idx})

        if selected is None:
            for i in info:
                i["selected"] = not i["hidden_in_file"]
        else:
            wanted = set(selected)
            for i in info:
                i["selected"] = i["name"] in wanted
        if not any(i["selected"] for i in info):
            raise ConversionError("no_sheets", "Choose at least one sheet to include.")

        # Visibility: only the selected sheets stay visible (LibreOffice skips hidden ones)
        first_visible = None
        for i in info:
            el = i["_el"]
            if i["selected"]:
                if "state" in el.attrib:
                    del el.attrib["state"]
                if first_visible is None:
                    first_visible = i["_idx"]
            else:
                el.set("state", "hidden")
        for view in wb.iter(_qn("workbookView")):
            view.set("activeTab", str(first_visible))
            view.set("firstSheet", str(first_visible))

        changed = {"xl/workbook.xml": etree.tostring(wb, xml_declaration=True, encoding="UTF-8", standalone=True)}

        for i in info:
            path = i["_path"]
            if not path or path not in names or i["kind"] != "sheet":
                continue
            try:
                root = etree.fromstring(zin.read(path), parser=etree.XMLParser(huge_tree=True))
            except etree.XMLSyntaxError:
                raise ConversionError("corrupt", f"Sheet \"{i['name']}\" is damaged.")
            if not i["selected"]:
                for sv in root.iter(_qn("sheetView")):
                    if sv.get("tabSelected"):
                        del sv.attrib["tabSelected"]
            else:
                if _apply_print_fit(root):
                    i["landscape"] = True
            changed[path] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename in changed:
                    zout.writestr(item, changed[item.filename], compress_type=zipfile.ZIP_DEFLATED)
                else:
                    zout.writestr(item, zin.read(item.filename))

    sheets = [{"name": i["name"], "hidden_in_file": i["hidden_in_file"],
               "selected": i["selected"], "kind": i["kind"]} for i in info]
    return sheets, notes


def _apply_print_fit(root) -> bool:
    """Fit the sheet to one page wide (rows flow onto further pages).

    Landscape is used when the table is wider than a portrait page, unless the
    workbook already says which orientation it wants. Returns True if landscape.
    """
    # <sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>
    sheet_pr = root.find(_qn("sheetPr"))
    if sheet_pr is None:
        sheet_pr = etree.Element(_qn("sheetPr"))
        root.insert(0, sheet_pr)
    psp = sheet_pr.find(_qn("pageSetUpPr"))
    if psp is None:
        psp = etree.Element(_qn("pageSetUpPr"))
        pos = 0
        for j, child in enumerate(sheet_pr):
            if etree.QName(child).localname in _SHEETPR_BEFORE:
                pos = j + 1
        sheet_pr.insert(pos, psp)
    had_fit = psp.get("fitToPage") in ("1", "true")
    psp.set("fitToPage", "1")

    ps = root.find(_qn("pageSetup"))
    if ps is None:
        ps = etree.Element(_qn("pageSetup"))
        pos = 0
        for j, child in enumerate(root):
            if etree.QName(child).localname in _BEFORE_PAGESETUP:
                pos = j + 1
        root.insert(pos, ps)

    height_fit = ps.get("fitToHeight")
    ps.set("fitToWidth", "1")
    if not had_fit or height_fit is None:
        ps.set("fitToHeight", "0")          # as many pages tall as needed
    if not ps.get("paperSize"):
        ps.set("paperSize", "9")            # A4 when the workbook doesn't say
    for attr in ("scale",):                  # fit replaces a fixed zoom
        if attr in ps.attrib:
            del ps.attrib[attr]

    orientation = ps.get("orientation")
    if orientation in ("landscape", "portrait"):
        return orientation == "landscape"

    try:
        pw, ph = _PAPER.get(int(ps.get("paperSize")), _PAPER[9])
    except ValueError:
        pw, ph = _PAPER[9]
    margins = root.find(_qn("pageMargins"))
    lm = rm = 0.7
    if margins is not None:
        try:
            lm, rm = float(margins.get("left", 0.7)), float(margins.get("right", 0.7))
        except ValueError:
            pass
    printable_portrait = pw - (lm + rm) * 72
    first, last = _used_columns(root)
    width = _content_width_pt(root, first, last)
    landscape = width > printable_portrait
    ps.set("orientation", "landscape" if landscape else "portrait")
    return landscape


# --------------------------------------------------------------------------
# Self-test (used at image build time, at server start and by /healthz)
# --------------------------------------------------------------------------

_MIN_DOCX = {
    "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
    "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
    "word/document.xml": '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>MCH self-test</w:t></w:r></w:p><w:p><w:r><w:t xml:space="preserve">&#x0627;&#x062E;&#x062A;&#x0628;&#x0627;&#x0631;</w:t></w:r></w:p></w:body></w:document>',
}
_MIN_XLSX = {
    "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
    "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Check" sheetId="1" r:id="rId1"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
    "xl/worksheets/sheet1.xml": '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>MCH self-test</t></is></c><c r="B1"><v>42</v></c></row></sheetData></worksheet>',
}


def _write_zip(path, parts):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)


def office_version() -> str | None:
    try:
        r = subprocess.run([_soffice_bin(), "--version"], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        return (r.stdout or "").strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def self_test() -> dict:
    """Convert a tiny Word document and workbook through the real pipeline."""
    result = {"office_version": office_version(), "word": False, "excel": False, "error": None}
    d = tempfile.mkdtemp(prefix="mch-selftest-")
    try:
        for kind, ext, parts in (("word", ".docx", _MIN_DOCX), ("excel", ".xlsx", _MIN_XLSX)):
            wd = os.path.join(d, kind)
            os.makedirs(wd)
            src = os.path.join(wd, "original" + ext)
            _write_zip(src, parts)
            res = convert(src, ext, wd)
            result[kind] = res.pages >= 1
    except ConversionError as e:
        result["error"] = f"{e.code}: {e.message}"
    except Exception as e:                       # report, never crash the caller
        result["error"] = f"{type(e).__name__}"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    result["ok"] = bool(result["office_version"] and result["word"] and result["excel"])
    return result


def cleanup_stale_profiles() -> int:
    """Remove LibreOffice profiles left behind by worker processes that no longer exist."""
    removed = 0
    base = tempfile.gettempdir()
    for name in os.listdir(base):
        if not name.startswith("mch-lo-profile-"):
            continue
        try:
            pid = int(name.rsplit("-", 1)[1])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
            continue                             # still running
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
        shutil.rmtree(os.path.join(base, name), ignore_errors=True)
        removed += 1
    return removed


if __name__ == "__main__":
    # `python converter.py --self-test` : exit code 0 only if Word and Excel conversion work
    import sys
    logging.basicConfig(level=logging.INFO)
    if "--self-test" in sys.argv:
        r = self_test()
        print(json.dumps(r))
        sys.exit(0 if r["ok"] else 1)
