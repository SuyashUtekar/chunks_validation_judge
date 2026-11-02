from __future__ import annotations

import io
import re
from typing import Any

from .ingestion import InputBundle


class EvidenceError(ValueError):
    """Source evidence could not be read safely."""


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _metadata_furniture(metadata: dict[str, Any] | None) -> dict[int, list[tuple[float, float, float, float]]]:
    result: dict[int, list[tuple[float, float, float, float]]] = {}
    if not metadata or not isinstance(metadata.get("pages"), list):
        return result
    furniture = {"header", "footer", "footnote", "page_number"}
    for page in metadata["pages"]:
        try:
            number = int(page["page_number"])
            width = float(page["width_pt"])
            height = float(page["height_pt"])
        except (KeyError, TypeError, ValueError):
            continue
        for element in page.get("elements", []):
            if str(element.get("type", "")).casefold() not in furniture:
                continue
            box = element.get("bbox")
            if not isinstance(box, dict):
                continue
            try:
                result.setdefault(number, []).append((
                    float(box["x0"]) * width, float(box["y0"]) * height,
                    float(box["x1"]) * width, float(box["y1"]) * height,
                ))
            except (KeyError, TypeError, ValueError):
                continue
    return result


def _intersection_over_block(rect: tuple[float, float, float, float],
                             block: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = block
    rx0, ry0, rx1, ry1 = rect
    overlap = max(0.0, min(x1, rx1) - max(x0, rx0)) * max(
        0.0, min(y1, ry1) - max(y0, ry0)
    )
    return overlap / max(1.0, (x1 - x0) * (y1 - y0))


def extract_pdf_units(bundle: InputBundle) -> tuple[list[dict[str, Any]], int]:
    try:
        import pymupdf
        pdf = pymupdf.open(stream=bundle.pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise EvidenceError(f"Could not read source PDF ({type(exc).__name__}).") from exc
    units: list[dict[str, Any]] = []
    furniture = _metadata_furniture(bundle.metadata)
    try:
        for page_index, page in enumerate(pdf, start=1):
            blocks = page.get_text("blocks")
            for block_index, block in enumerate(blocks, start=1):
                if len(block) < 5:
                    continue
                text = _clean(block[4])
                if not text or (len(block) > 6 and int(block[6]) != 0):
                    continue
                rect = (float(block[0]), float(block[1]), float(block[2]), float(block[3]))
                if any(_intersection_over_block(box, rect) >= 0.35
                       for box in furniture.get(page_index, [])):
                    continue
                units.append({
                    "unit_id": f"pdf:p{page_index}:b{block_index}",
                    "page_number": page_index,
                    "text": text,
                    "kind": "pdf_block",
                    "section_path": [],
                    "bbox": [round(value, 2) for value in rect],
                })
        return units, pdf.page_count
    finally:
        pdf.close()


def extract_docx_units(docx_bytes: bytes | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not docx_bytes:
        return [], []
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P

        document = Document(io.BytesIO(docx_bytes))
        units: list[dict[str, Any]] = []
        headings: list[dict[str, Any]] = []
        stack: list[tuple[int, str]] = []
        sequence = 0
        for child in document.element.body.iterchildren():
            block: Any
            if isinstance(child, CT_P):
                block = Paragraph(child, document)
            elif isinstance(child, CT_Tbl):
                block = Table(child, document)
            else:
                continue
            if isinstance(block, Paragraph):
                text = _clean(block.text)
                if not text:
                    continue
                style_name = block.style.name if block.style is not None else ""
                match = re.fullmatch(r"Heading\s+(\d+)", style_name)
                if style_name == "Title":
                    headings.append({"title": text, "level": 0, "section_path": []})
                    continue
                if match:
                    level = max(1, int(match.group(1)))
                    while stack and stack[-1][0] >= level:
                        stack.pop()
                    stack.append((level, text))
                    path = [title for _depth, title in stack]
                    headings.append({"title": text, "level": level, "section_path": path.copy()})
                    sequence += 1
                    units.append({"unit_id": f"docx:p{sequence}", "page_number": None,
                                  "text": text, "kind": "heading", "section_path": path})
                    continue
                path = [title for _depth, title in stack]
                sequence += 1
                units.append({"unit_id": f"docx:p{sequence}", "page_number": None,
                              "text": text, "kind": "paragraph", "section_path": path})
            elif isinstance(block, Table):
                for row_index, row in enumerate(block.rows, start=1):
                    cells = [_clean(cell.text) for cell in row.cells]
                    text = " | ".join(value for value in cells if value)
                    if not text:
                        continue
                    sequence += 1
                    units.append({"unit_id": f"docx:t{sequence}:r{row_index}", "page_number": None,
                                  "text": text, "kind": "table_row",
                                  "section_path": [title for _depth, title in stack]})
        return units, headings
    except Exception as exc:
        raise EvidenceError(f"Could not read converted DOCX ({type(exc).__name__}).") from exc


def build_source_evidence(bundle: InputBundle) -> dict[str, Any]:
    pdf_units, page_count = extract_pdf_units(bundle)
    docx_units, sections = extract_docx_units(bundle.docx_bytes)
    return {
        "pdf_units": pdf_units,
        "docx_units": docx_units,
        "sections": sections,
        "page_count": page_count,
        "docx_available": bundle.docx_bytes is not None,
        "pdf_text_available": bool(pdf_units),
        "docx_text_available": bool(docx_units),
    }
