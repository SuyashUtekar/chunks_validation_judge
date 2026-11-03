from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .config import settings


class InputValidationError(ValueError):
    """An uploaded evaluation artifact does not meet the input contract."""


@dataclass
class InputBundle:
    pdf_bytes: bytes
    docx_bytes: bytes | None
    metadata: dict[str, Any] | None
    package_members: list[str]
    pdf_name: str
    docx_name: str | None
    package_sha256: str


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe_zip_name(name: str) -> bool:
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in name


def load_pdflens_package(raw: bytes, source_pdf_bytes: bytes | None = None,
                        source_pdf_name: str | None = None) -> InputBundle:
    if len(raw) > settings.max_package_bytes:
        raise InputValidationError("PDFLens package exceeds the configured size limit.")
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise InputValidationError("PDFLens package must be a valid ZIP file.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        infos = archive.infolist()
        if len(infos) > settings.max_zip_members:
            raise InputValidationError("PDFLens package has too many members.")
        if sum(info.file_size for info in infos) > settings.max_uncompressed_bytes:
            raise InputValidationError("PDFLens package expands beyond the configured limit.")
        names = [info.filename for info in infos]
        if any(not _safe_zip_name(name) for name in names):
            raise InputValidationError("PDFLens package contains an unsafe member path.")
        if len({name.casefold() for name in names}) != len(names):
            raise InputValidationError("PDFLens package contains duplicate member names.")
        if archive.testzip() is not None:
            raise InputValidationError("PDFLens package contains a corrupt ZIP member.")
        files = {info.filename: info for info in infos if not info.is_dir()}
        pdf_names = [name for name in files if name.casefold().endswith(".pdf")]
        docx_names = [name for name in files if name.casefold().endswith(".docx")]
        metadata_names = [name for name in files if PurePosixPath(name).name.casefold() == "metadata.json"]
        if len(pdf_names) > 1:
            raise InputValidationError("Package must contain at most one source PDF.")
        if len(docx_names) > 1:
            raise InputValidationError("Package must contain at most one DOCX.")
        embedded_pdf = archive.read(pdf_names[0]) if pdf_names else None
        if embedded_pdf and source_pdf_bytes and sha256(embedded_pdf) != sha256(source_pdf_bytes):
            raise InputValidationError("The separately uploaded source PDF does not match the PDF inside the ZIP.")
        pdf_bytes = embedded_pdf or source_pdf_bytes
        pdf_name = pdf_names[0] if pdf_names else (source_pdf_name or "source.pdf")
        if pdf_bytes is None:
            raise InputValidationError(
                "The ZIP has no source PDF. Upload the original PDF separately to run evidence-based validation."
            )
        if not pdf_bytes.startswith(b"%PDF-"):
            raise InputValidationError("The source PDF does not have a valid PDF signature.")
        docx_name = docx_names[0] if docx_names else None
        docx_bytes = archive.read(docx_name) if docx_name else None
        metadata = None
        if metadata_names:
            if len(metadata_names) != 1:
                raise InputValidationError("Package must contain at most one metadata.json.")
            try:
                metadata = json.loads(archive.read(metadata_names[0]))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InputValidationError("Package metadata.json is not valid JSON.") from exc
        _validate_manifest(archive, files, source_pdf_bytes, source_pdf_name)
        return InputBundle(pdf_bytes, docx_bytes, metadata, names, pdf_name, docx_name, sha256(raw))
    except zipfile.BadZipFile as exc:
        raise InputValidationError("PDFLens package is not a readable ZIP file.") from exc


def _validate_manifest(archive: zipfile.ZipFile, files: dict[str, zipfile.ZipInfo],
                       external_pdf_bytes: bytes | None = None,
                       external_pdf_name: str | None = None) -> None:
    manifest_names = [name for name in files if PurePosixPath(name).name.casefold() == "manifest.json"]
    if not manifest_names:
        return
    if len(manifest_names) != 1:
        raise InputValidationError("Package contains multiple manifests.")
    try:
        manifest = json.loads(archive.read(manifest_names[0]))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputValidationError("Package manifest is not valid JSON.") from exc
    members = manifest.get("members", []) if isinstance(manifest, dict) else []
    for member in members:
        if not isinstance(member, dict):
            raise InputValidationError("Package manifest has an invalid member entry.")
        name = member.get("path")
        expected = member.get("sha256")
        if not isinstance(name, str) or not _safe_zip_name(name):
            raise InputValidationError("Package manifest references a missing or unsafe member.")
        if name in files:
            actual = sha256(archive.read(name))
        elif (external_pdf_bytes is not None and external_pdf_name
              and name.casefold().endswith(".pdf")
              and PurePosixPath(name).name.casefold() == PurePosixPath(external_pdf_name).name.casefold()):
            actual = sha256(external_pdf_bytes)
        else:
            raise InputValidationError("Package manifest references a missing or unsafe member.")
        if expected and actual.casefold() != str(expected).casefold():
            raise InputValidationError(f"Package checksum mismatch for {name}.")


def _key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def _read_rows(raw: bytes, filename: str) -> list[dict[str, Any]]:
    suffix = PurePosixPath(filename).suffix.casefold()
    if suffix == ".json":
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InputValidationError("Chunk JSON is not valid JSON.") from exc
        if isinstance(payload, dict):
            payload = payload.get("chunks", payload.get("data"))
        if not isinstance(payload, list):
            raise InputValidationError("Chunk JSON must be a list or contain a 'chunks' list.")
        if not all(isinstance(item, dict) for item in payload):
            raise InputValidationError("Each chunk JSON entry must be an object.")
        return [{_key(key): value for key, value in item.items()} for item in payload]
    if suffix == ".csv":
        try:
            text = raw.decode("utf-8-sig")
            return [{_key(key): value for key, value in row.items() if key is not None}
                    for row in csv.DictReader(io.StringIO(text))]
        except UnicodeDecodeError as exc:
            raise InputValidationError("Chunk CSV must be UTF-8 encoded.") from exc
    if suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            sheet = workbook.active
            rows = sheet.iter_rows(values_only=True)
            headers = next(rows, None)
            if not headers:
                return []
            keys = [_key(value) for value in headers]
            result = []
            for row in rows:
                if not any(value is not None for value in row):
                    continue
                result.append({keys[index]: value for index, value in enumerate(row)
                               if index < len(keys) and keys[index]})
            workbook.close()
            return result
        except Exception as exc:
            raise InputValidationError("Could not read the uploaded chunk workbook.") from exc
    raise InputValidationError("Chunks must be supplied as .json, .xlsx, or .csv.")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"\s*(?:\||>)\s*", value) if part.strip()]
    if isinstance(value, (tuple, list)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()] if str(value).strip() else []


def normalize_chunks(raw: bytes, filename: str) -> list[dict[str, Any]]:
    if len(raw) > settings.max_chunks_bytes:
        raise InputValidationError("Chunk file exceeds the configured size limit.")
    rows = _read_rows(raw, filename)
    file_metadata: dict[str, Any] = {}
    if PurePosixPath(filename).suffix.casefold() == ".json":
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InputValidationError("Chunk JSON is not valid JSON.") from exc
        if isinstance(payload, dict):
            file_metadata = {key: payload[key] for key in ("source_docx", "document_title", "document", "chunk_count", "max_words", "overlap_words") if key in payload}
    if not rows:
        raise InputValidationError("Chunk file contains no chunk records.")
    if len(rows) > settings.max_chunks:
        raise InputValidationError("Chunk file exceeds the configured chunk count limit.")
    chunks = []
    for index, row in enumerate(rows, start=1):
        chunk_id = row.get("chunk_id", row.get("id"))
        content = row.get("chunk_text", row.get("content", row.get("text")))
        section_value = row.get("section_path", row.get("section", []))
        section = section_value if isinstance(section_value, dict) else {}
        path = _string_list(section.get("path") if section else section_value)
        title = row.get("section_title") or (section.get("title") if section else None)
        if title and (not path or path[-1].casefold() != str(title).casefold()):
            path.append(str(title).strip())
        sequence = row.get("sequence_number", row.get("sequence", row.get("chunk_index")))
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        pages = row.get("page_numbers", source.get("page_numbers", source.get("pages", [])))
        if isinstance(pages, (int, float, str)):
            pages = [pages]
        try:
            pages = sorted({int(page) for page in pages if str(page).strip()})
        except (TypeError, ValueError):
            pages = []
        try:
            seq_number = int(sequence) if sequence not in (None, "") else None
        except (TypeError, ValueError):
            seq_number = None
        content_string = "" if content is None else str(content)
        chunks.append({
            "row_number": index,
            "chunk_id": str(chunk_id).strip() if chunk_id is not None else "",
            "content": content_string,
            "section_path": path,
            "section_id": str(row.get("section_id") or (section.get("section_id") if section else "") or ""),
            "sequence_number": seq_number,
            "page_numbers": pages,
            "start_offset": row.get("start_offset", source.get("start_offset")),
            "end_offset": row.get("end_offset", source.get("end_offset")),
            "document_id": str(row.get("document_id") or ""),
            "document": str(row.get("document") or ""),
            "document_title": str(row.get("document_title") or file_metadata.get("document_title") or ""),
            "source_docx": str(row.get("source_docx") or file_metadata.get("source_docx") or ""),
            "section_level": row.get("section_level"),
            "part": row.get("part"),
            "word_count": row.get("word_count"),
            "chunk_file_metadata": file_metadata,
            "job_id": str(row.get("job_id", row.get("chunking_job_id")) or ""),
            "strategy": str(row.get("chunking_strategy") or row.get("strategy") or ""),
            "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
            "entities": row.get("entities") if isinstance(row.get("entities"), list) else [],
            "raw": row,
        })
    return chunks


def parse_config(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputValidationError("Chunking configuration must be valid JSON.") from exc
    if not isinstance(config, dict):
        raise InputValidationError("Chunking configuration must be a JSON object.")
    for name in ("max_words", "max_characters", "overlap_words"):
        value = config.get(name)
        if value is not None and (not isinstance(value, int) or value < 0
                                  or (name in {"max_words", "max_characters"} and value == 0)):
            qualifier = "positive" if name in {"max_words", "max_characters"} else "non-negative"
            raise InputValidationError(f"Configuration field {name} must be a {qualifier} integer.")
    return config
