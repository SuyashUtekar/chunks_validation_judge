"""Create section-aware text chunks from a Word document or PDFLens ZIP.

Run from the project root with:
    uv run python chunking_strategy/sectionwise_chunking.py path/to/file.docx

The output is JSON beside the input unless ``--output`` is supplied.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Iterable

try:
    from docx import Document
    from docx.document import Document as DocumentType
    from docx.table import Table
    from docx.text.paragraph import Paragraph
except ImportError as exc:  # pragma: no cover - exercised by the CLI error path
    raise SystemExit(
        "python-docx is required. Install project dependencies with `uv sync`, "
        "then run this script with `uv run python`."
    ) from exc


@dataclass
class Section:
    """A heading and the content that belongs directly to it."""

    title: str
    level: int
    path: list[str]
    blocks: list[str] = field(default_factory=list)


def _heading_level(paragraph: Paragraph) -> int | None:
    """Resolve built-in and inherited Word heading styles to a 1-based level."""
    style = paragraph.style
    visited: set[str] = set()
    while style is not None and style.style_id not in visited:
        visited.add(style.style_id)
        for candidate in (style.style_id or "", style.name or ""):
            match = re.fullmatch(r"Heading\s*(\d+)", candidate, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        style = style.base_style

    # Word outlineLvl is zero-based. It can be set directly on a paragraph or
    # inherited from a paragraph style.
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    ppr = paragraph._p.pPr
    outline = ppr.find(f"{ns}outlineLvl") if ppr is not None else None
    style = paragraph.style
    while outline is None and style is not None:
        style_ppr = style.element.pPr
        outline = style_ppr.find(f"{ns}outlineLvl") if style_ppr is not None else None
        style = style.base_style
    if outline is not None:
        try:
            value = int(outline.get(f"{ns}val"))
            return value + 1 if 0 <= value <= 8 else None
        except (TypeError, ValueError):
            return None
    return None


def _paragraph_has_image(paragraph: Paragraph) -> bool:
    # python-docx's XPath helper does not support namespace prefixes on every
    # supported release, so inspect the already parsed paragraph XML directly.
    xml = paragraph._p.xml
    return "<w:drawing" in xml or "<w:pict" in xml


def _table_to_text(table: Table) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells = [re.sub(r"\s+", " ", cell.text).strip() for cell in row.cells]
        if any(cells):
            rows.append(" ; ".join(cells))
    return "\n".join(rows)


def _iter_blocks(document: DocumentType) -> Iterable[tuple[str, str, int | None]]:
    """Yield document-order blocks as (kind, text, heading_level)."""
    image_number = 0
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            style_name = block.style.name if block.style is not None else ""
            if style_name.casefold() == "title" or (block.style and block.style.style_id == "Title"):
                if text:
                    yield "title", text, None
                continue

            level = _heading_level(block)
            if level is not None:
                if text:
                    yield "heading", text, level
                continue

            if text:
                yield "text", text, None
            if _paragraph_has_image(block):
                image_number += 1
                yield "image", f"[Embedded image {image_number}]", None
        elif isinstance(block, Table):
            text = _table_to_text(block)
            if text:
                yield "table", text, None


def _chunk_document(
    document: DocumentType,
    source_name: str,
    source_docx: str,
) -> dict[str, object]:
    """Chunk a parsed DOCX using its Word heading hierarchy."""
    title = ""
    sections: list[Section] = []
    active: Section | None = None
    heading_stack: list[tuple[int, str]] = []
    preamble: list[str] = []

    def flush() -> None:
        nonlocal active
        if active is not None:
            sections.append(active)
            active = None

    for kind, text, level in _iter_blocks(document):
        if kind == "title":
            title = text
            continue
        if kind == "heading":
            flush()
            assert level is not None
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            active = Section(
                title=text,
                level=level,
                path=[heading for _, heading in heading_stack],
            )
            continue
        if active is None:
            if heading_stack:
                active = Section(
                    title=heading_stack[-1][1],
                    level=heading_stack[-1][0],
                    path=[heading for _, heading in heading_stack],
                )
            else:
                preamble.append(text)
                continue
        active.blocks.append(text)
    flush()

    if preamble:
        sections.insert(0, Section(title="Introduction", level=0, path=[], blocks=preamble))

    chunks: list[dict[str, object]] = []
    for section in sections:
        section_text = "\n\n".join(section.blocks).strip()
        if not section_text:
            continue
        chunk_prefix = section.path if section.path else ([title] if title else [])
        chunk_text = " | ".join([*chunk_prefix, section_text])
        chunks.append(
            {
                "chunk_id": f"chunk-{len(chunks) + 1:06d}",
                "document": source_name,
                "document_title": title or Path(source_name).stem,
                "section_title": section.title,
                "section_level": section.level,
                "section_path": section.path,
                "part": 1,
                "body_text": section_text,
                "text": chunk_text,
                "word_count": len(chunk_text.split()),
            }
        )

    return {
        "source_docx": source_docx,
        "document_title": title or Path(source_name).stem,
        "chunk_count": len(chunks),
        "chunks": chunks,
    }


def chunk_docx(
    source: Path,
) -> dict[str, object]:
    """Read a DOCX path and return chunks grouped by its Word heading hierarchy."""
    source = Path(source).expanduser().resolve()
    return _chunk_document(Document(str(source)), source.name, str(source))


def chunk_docx_bytes(
    raw: bytes,
    source_name: str = "document.docx",
) -> dict[str, object]:
    """Chunk uploaded DOCX bytes without writing the source document to disk."""
    try:
        document = Document(BytesIO(raw))
    except Exception as exc:
        raise ValueError("The package DOCX is invalid or unreadable.") from exc
    return _chunk_document(document, Path(source_name).name, source_name)


def _find_docx_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    """Return the sole DOCX in a PDFLens ZIP, rejecting ambiguous packages."""
    members = [
        item for item in archive.infolist()
        if not item.is_dir() and item.filename.lower().endswith(".docx")
    ]
    if not members:
        raise ValueError("The ZIP does not contain a .docx file.")
    if len(members) > 1:
        names = ", ".join(item.filename for item in members)
        raise ValueError(f"Expected one DOCX in the ZIP; found {len(members)}: {names}")
    return members[0]


def chunk_pdflens_zip(
    source_zip: Path,
) -> dict[str, object]:
    """Chunk the single DOCX inside a PDFLens ZIP without extracting the archive."""
    source_zip = source_zip.expanduser().resolve()
    if not source_zip.is_file():
        raise FileNotFoundError(f"ZIP file does not exist: {source_zip}")
    if source_zip.suffix.lower() != ".zip":
        raise ValueError(f"Expected a .zip input file: {source_zip}")

    try:
        with zipfile.ZipFile(source_zip) as archive:
            member = _find_docx_member(archive)
            with tempfile.TemporaryDirectory(prefix="pdflens-chunk-") as temporary_dir:
                temporary_docx = Path(temporary_dir) / Path(member.filename).name
                with archive.open(member) as zipped_docx, temporary_docx.open("wb") as output:
                    shutil.copyfileobj(zipped_docx, output)
                result = chunk_docx(temporary_docx)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Input is not a valid ZIP archive: {source_zip}") from exc

    result["source_zip"] = str(source_zip)
    result["source_docx"] = member.filename
    result["document"] = Path(member.filename).name
    return result


def save_chunks_xlsx(chunks: list[dict[str, object]], destination: Path) -> None:
    """Write the requested two-column Excel view of the generated chunks."""
    try:
        from openpyxl import Workbook as ExcelWorkbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as exc:  # pragma: no cover - exercised by the CLI error path
        raise SystemExit(
            "openpyxl is required for Excel output. Install project dependencies with `uv sync`."
        ) from exc

    workbook = ExcelWorkbook()
    sheet = workbook.active
    sheet.title = "Chunks"
    sheet.append(["chunk_id", "chunk_text"])
    for chunk in chunks:
        sheet.append([chunk["chunk_id"], chunk["text"]])

    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.column_dimensions["A"].width = 20
    sheet.column_dimensions["B"].width = 100
    sheet.row_dimensions[1].height = 24
    for row_number in range(2, sheet.max_row + 1):
        text = str(sheet.cell(row=row_number, column=2).value or "")
        wrapped_lines = sum(max(1, (len(line) + 98) // 99) for line in text.splitlines())
        sheet.row_dimensions[row_number].height = max(30, min(wrapped_lines * 15 + 8, 600))
        for cell in sheet[row_number]:
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(destination)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split a DOCX or a PDFLens ZIP containing one DOCX into section chunks."
    )
    parser.add_argument("input", type=Path, help="Path to a DOCX or PDFLens ZIP file")
    parser.add_argument(
        "--output", "-o", type=Path, help="Output JSON path (defaults beside the DOCX)"
    )
    parser.add_argument(
        "--excel-output",
        type=Path,
        help="Excel output path (defaults beside the JSON output)",
    )
    args = parser.parse_args()

    source = args.input.expanduser().resolve()
    if not source.is_file():
        parser.error(f"Input file does not exist: {source}")
    if source.suffix.lower() not in {".docx", ".zip"}:
        parser.error(f"Expected a .docx or .zip file: {source}")

    output = args.output.expanduser().resolve() if args.output else source.with_name(
        f"{source.stem}_chunks.json"
    )
    if source.suffix.lower() == ".zip":
        result = chunk_pdflens_zip(source)
    else:
        result = chunk_docx(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    excel_output = args.excel_output.expanduser().resolve() if args.excel_output else output.with_suffix(
        ".xlsx"
    )
    save_chunks_xlsx(result["chunks"], excel_output)
    print(f"Source: {source}")
    print(f"Chunks: {result['chunk_count']}")
    print(f"Saved: {output}")
    print(f"Saved: {excel_output}")


if __name__ == "__main__":
    main()
