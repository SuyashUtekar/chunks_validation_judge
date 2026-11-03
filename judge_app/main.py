from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .config import settings
from .evaluation import evaluate
from .ingestion import InputValidationError, load_pdflens_package, normalize_chunks, parse_config, sha256
from .reporting import build_report_xlsx, report_json_bytes

logger = logging.getLogger("chunk_quality_judge")
app = FastAPI(
    title="Section-wise Chunk Quality Judge",
    version="0.1.0",
    description=("Standalone evidence-based evaluation of Intelligent Search chunks against "
                 "the PDFLens PDF/DOCX package."),
)


def _evaluation_dir(evaluation_id: str) -> Path:
    if not re.fullmatch(r"chunk-eval-[0-9a-f-]{36}", evaluation_id):
        raise HTTPException(404, detail={"code": "EVALUATION_NOT_FOUND", "message": "Evaluation not found."})
    path = settings.data_dir / evaluation_id
    if not path.is_dir():
        raise HTTPException(404, detail={"code": "EVALUATION_NOT_FOUND", "message": "Evaluation not found."})
    return path


def _read_report(evaluation_id: str) -> dict[str, Any]:
    folder = _evaluation_dir(evaluation_id)
    try:
        return json.loads((folder / "report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, detail={"code": "REPORT_UNAVAILABLE", "message": "Evaluation report is unavailable."}) from exc


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "section-wise-chunk-quality-judge"}


@app.post("/api/v1/chunk-evaluations", status_code=201)
async def create_evaluation(
    pdflens_package: UploadFile = File(...),
    chunks_file: UploadFile = File(...),
    source_pdf: UploadFile | None = File(None),
    chunking_config: str = Form("{}"),
) -> JSONResponse:
    package_limit = settings.max_package_bytes + 1
    chunks_limit = settings.max_chunks_bytes + 1
    package_bytes = await pdflens_package.read(package_limit)
    chunks_bytes = await chunks_file.read(chunks_limit)
    source_pdf_bytes = await source_pdf.read(settings.max_package_bytes + 1) if source_pdf else None
    if len(package_bytes) >= package_limit:
        raise HTTPException(413, detail={"code": "PACKAGE_TOO_LARGE", "message": "PDFLens package exceeds the configured limit."})
    if len(chunks_bytes) >= chunks_limit:
        raise HTTPException(413, detail={"code": "CHUNKS_TOO_LARGE", "message": "Chunk input exceeds the configured limit."})
    if source_pdf_bytes is not None and len(source_pdf_bytes) > settings.max_package_bytes:
        raise HTTPException(413, detail={"code": "SOURCE_PDF_TOO_LARGE", "message": "Source PDF exceeds the configured limit."})
    try:
        package = load_pdflens_package(package_bytes, source_pdf_bytes,
                                       Path(source_pdf.filename or "source.pdf").name if source_pdf else None)
        chunks = normalize_chunks(chunks_bytes, chunks_file.filename or "chunks.json")
        config = parse_config(chunking_config)
    except InputValidationError as exc:
        raise HTTPException(422, detail={"code": "INVALID_EVALUATION_INPUT", "message": str(exc)}) from exc

    try:
        report = await run_in_threadpool(evaluate, package, chunks, config, sha256(chunks_bytes))
        report["inputs"]["chunks_filename"] = Path(chunks_file.filename or "chunks").name
        report["inputs"]["pdflens_package_filename"] = Path(pdflens_package.filename or "package.zip").name
        report_json = report_json_bytes(report)
        report_xlsx = await run_in_threadpool(build_report_xlsx, report)
        target = settings.data_dir / report["evaluation_id"]
        target.mkdir(parents=True, exist_ok=False)
        temporary_json = target / ".report.json.tmp"
        temporary_xlsx = target / ".report.xlsx.tmp"
        temporary_json.write_bytes(report_json)
        temporary_xlsx.write_bytes(report_xlsx)
        temporary_json.replace(target / "report.json")
        temporary_xlsx.replace(target / "report.xlsx")
    except HTTPException:
        raise
    except Exception as exc:
        error_id = str(uuid.uuid4())
        logger.exception("evaluation failed request_id=%s error_type=%s", error_id, type(exc).__name__)
        # Do not include uploaded text or exception messages in the API response.
        raise HTTPException(500, detail={"code": "EVALUATION_FAILED", "request_id": error_id,
                                         "message": f"Evaluation failed ({type(exc).__name__})."}) from exc
    return JSONResponse(status_code=201, content={
        "evaluation_id": report["evaluation_id"],
        "status": report["status"],
        "quality_gate": report["quality_gate"],
        "summary": report["summary"],
        "report_url": f"/api/v1/chunk-evaluations/{report['evaluation_id']}/report",
        "workbook_url": f"/api/v1/chunk-evaluations/{report['evaluation_id']}/report.xlsx",
    })


@app.get("/api/v1/chunk-evaluations/{evaluation_id}")
def get_evaluation_status(evaluation_id: str) -> dict[str, Any]:
    report = _read_report(evaluation_id)
    return {"evaluation_id": evaluation_id, "status": report["status"],
            "quality_gate": report["quality_gate"], "created_at": report["created_at"],
            "summary": report["summary"], "llm": report["llm"]}


@app.get("/api/v1/chunk-evaluations/{evaluation_id}/report")
def get_document_report(evaluation_id: str) -> dict[str, Any]:
    return _read_report(evaluation_id)


@app.get("/api/v1/chunk-evaluations/{evaluation_id}/report.xlsx")
def download_report(evaluation_id: str) -> FileResponse:
    folder = _evaluation_dir(evaluation_id)
    path = folder / "report.xlsx"
    if not path.is_file():
        raise HTTPException(404, detail={"code": "REPORT_NOT_FOUND", "message": "Workbook not found."})
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        filename=f"{evaluation_id}_quality_report.xlsx")


@app.get("/api/v1/chunk-evaluations/{evaluation_id}/chunks/{chunk_id}")
def get_chunk_report(evaluation_id: str, chunk_id: str) -> dict[str, Any]:
    report = _read_report(evaluation_id)
    chunk = next((item for item in report["chunks"] if item["chunk_id"] == chunk_id), None)
    if chunk is None:
        raise HTTPException(404, detail={"code": "CHUNK_NOT_FOUND", "message": "Chunk not found."})
    return {"evaluation_id": evaluation_id, "chunk": chunk,
            "findings": [item for item in report["findings"] if item["chunk_id"] == chunk_id]}


@app.get("/api/v1/chunk-evaluations/{evaluation_id}/sections/{section_id:path}")
def get_section_report(evaluation_id: str, section_id: str) -> dict[str, Any]:
    report = _read_report(evaluation_id)
    section = next((item for item in report["sections"] if item["section_id"] == section_id), None)
    if section is None:
        raise HTTPException(404, detail={"code": "SECTION_NOT_FOUND", "message": "Section not found."})
    chunk_ids = {item["chunk_id"] for item in report["chunks"]
                 if item["section_path"][:len(section["section_path"])] == section["section_path"]}
    return {"evaluation_id": evaluation_id, "section": section,
            "chunks": [item for item in report["chunks"] if item["chunk_id"] in chunk_ids],
            "findings": [item for item in report["findings"] if item["chunk_id"] in chunk_ids]}


@app.get("/api/v1/chunk-evaluations/{evaluation_id}/coverage")
def get_coverage_report(evaluation_id: str) -> dict[str, Any]:
    report = _read_report(evaluation_id)
    return {"evaluation_id": evaluation_id, "coverage": report["coverage"],
            "source_coverage_percentage": report["summary"]["source_coverage_percentage"]}


@app.on_event("startup")
def prepare_storage() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
