"""
Builds the final combined PDF from the user's page sequence.

Pages are copied from the converted source PDFs with pypdf - they are never
re-rendered, so text stays selectable and images keep their original quality.
Rotation only changes the page's /Rotate value.
"""

from __future__ import annotations

from pypdf import PdfReader, PdfWriter


class BuildError(Exception):
    pass


def build(sequence: list[dict], sources: dict[str, str], out_path: str, title: str = "") -> int:
    """sequence: [{"file": fid, "page": 0-based index, "rotate": 0/90/180/270}]
    sources:  {fid: path to that file's converted.pdf}
    Returns the number of pages written (always == len(sequence)).
    """
    if not sequence:
        raise BuildError("No pages are selected.")
    readers: dict[str, PdfReader] = {}
    writer = PdfWriter()
    for i, item in enumerate(sequence):
        fid = item.get("file")
        if fid not in sources:
            raise BuildError("One of the documents is no longer available. Please re-add it.")
        if fid not in readers:
            r = PdfReader(sources[fid], strict=False)
            if r.is_encrypted:
                r.decrypt("")
            readers[fid] = r
        reader = readers[fid]
        try:
            idx = int(item.get("page"))
            rot = int(item.get("rotate", 0)) % 360
        except (TypeError, ValueError):
            raise BuildError("The page list is invalid.")
        if not 0 <= idx < len(reader.pages):
            raise BuildError("A page in the list doesn't exist in its document any more.")
        if rot not in (0, 90, 180, 270):
            raise BuildError("Pages can only be rotated in 90 degree steps.")
        page = writer.add_page(reader.pages[idx])
        if rot:
            page.rotate(rot)

    writer.add_metadata({"/Title": title or "MCH Combined Document",
                         "/Producer": "MCH PDF Organizer"})
    with open(out_path, "wb") as f:
        writer.write(f)

    # Verify what was written opens and has exactly the requested pages
    check = PdfReader(out_path, strict=False)
    if len(check.pages) != len(sequence):
        raise BuildError("The combined PDF didn't come out with the expected number of pages.")
    return len(check.pages)
