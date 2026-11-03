from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_SUBHEADER_FILL = PatternFill("solid", fgColor="D9EAF7")
_OUTCOME_FILLS = {
    "FAIL": PatternFill("solid", fgColor="FCE4D6"),
    "INCONCLUSIVE": PatternFill("solid", fgColor="FFF2CC"),
}
_MAX_EVIDENCE_ROWS = 40
_MAX_REFERENCES = 8
_MAX_EXCERPT_CHARS = 220


def _excel_safe(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (list, dict, tuple)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def _short(value: Any, limit: int = _MAX_EXCERPT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _primary_criterion_ids(report: dict[str, Any]) -> set[str]:
    return {
        criterion_id
        for dimension in report.get("scorecard", {}).get("dimensions", [])
        for criterion_id in dimension.get("criteria", [])
    }


def _evidence_to_review(report: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    primary_ids = _primary_criterion_ids(report)
    rows = [
        item for item in report.get("findings", [])
        if item.get("criterion_id") in primary_ids
        and item.get("outcome") in {"FAIL", "INCONCLUSIVE"}
    ]
    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, None: 4}
    rows.sort(key=lambda item: (
        0 if item.get("outcome") == "FAIL" else 1,
        severity_rank.get(item.get("severity"), 4),
        item.get("chunk_id") or "",
        item.get("criterion_id") or "",
    ))
    compact = []
    for item in rows[:_MAX_EVIDENCE_ROWS]:
        compact.append({
            "chunk_id": item.get("chunk_id") or None,
            "criterion": item.get("criterion"),
            "outcome": item.get("outcome"),
            "severity": item.get("severity"),
            "explanation": _short(item.get("explanation")),
            "source_refs": (item.get("source_evidence_refs") or [])[:_MAX_REFERENCES],
            "chunk_refs": (item.get("chunk_evidence_refs") or [])[:_MAX_REFERENCES],
            "source_excerpt": _short(item.get("source_excerpt")),
            "chunk_excerpt": _short(item.get("chunk_excerpt")),
        })
    return compact, max(0, len(rows) - len(compact))


def _compact_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    actions = []
    for item in report.get("action_plan", {}).get("actions", [])[:10]:
        chunks = item.get("chunk_ids") or []
        sources = item.get("source_refs") or []
        actions.append({
            "priority": item.get("priority"),
            "issue": item.get("title"),
            "next_step": _short(item.get("what_to_do"), 320),
            "affected_chunk_count": len(chunks),
            "chunk_ids": chunks[:_MAX_REFERENCES],
            "source_refs": sources[:_MAX_REFERENCES],
            "pages": (item.get("pages") or [])[:_MAX_REFERENCES],
            "criteria": (item.get("criterion_ids") or [])[:_MAX_REFERENCES],
        })
    return actions


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Return a concise, evidence-backed report for normal download/API use."""
    summary = report.get("summary", {})
    metadata = report.get("chunk_file_metadata", {})
    evidence, omitted = _evidence_to_review(report)
    source_docx = metadata.get("source_docx") or report.get("inputs", {}).get("source_docx_member")
    input_docx = report.get("inputs", {}).get("source_docx_member")
    scorecard = [
        {
            "dimension": item.get("dimension"),
            "score_percent": item.get("pass_percentage"),
            "result": item.get("status"),
            "what_it_checks": item.get("what_it_checks"),
            "scorable_checks": item.get("scorable_count"),
            "failed_checks": item.get("fail_count"),
            "unresolved_checks": item.get("inconclusive_count"),
        }
        for item in report.get("scorecard", {}).get("dimensions", [])
    ]
    return {
        "report_version": "concise-1",
        "evaluation_id": report.get("evaluation_id"),
        "created_at": report.get("created_at"),
        "quality_gate": report.get("quality_gate"),
        "what_this_means": report.get("action_plan", {}).get("summary"),
        "document": {
            "title": metadata.get("document_title") or report.get("document_id"),
            "source_docx": Path(str(source_docx)).name if source_docx else input_docx,
            "chunk_file": report.get("inputs", {}).get("chunks_filename"),
            "chunk_count": summary.get("total_chunks"),
            "strategy": report.get("chunking_strategy"),
            "max_words": metadata.get("max_words"),
            "overlap_words": metadata.get("overlap_words"),
        },
        "overall_score_percent": summary.get("overall_score"),
        "scorecard": scorecard,
        "coverage": {
            "docx_source_coverage_percent": summary.get("source_coverage_percentage"),
            "covered_source_units": summary.get("covered_source_units"),
            "eligible_source_units": summary.get("eligible_source_units"),
            "docx_units_not_in_chunks": summary.get("missing_source_units"),
            "possible_pdf_to_docx_omissions": summary.get("pdf_docx_discrepancies"),
        },
        "next_steps": _compact_actions(report),
        "evidence_to_review": evidence,
        "evidence_rows_omitted": omitted,
        "judge": {
            "criteria_version": report.get("criteria_version"),
            "prompt_version": report.get("llm", {}).get("prompt_version"),
            "model": report.get("llm", {}).get("model"),
        },
    }


def _style_header(sheet: Any, row_number: int = 1) -> None:
    for cell in sheet[row_number]:
        cell.fill = _HEADER_FILL
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[row_number].height = 30


def _style_body(sheet: Any, min_row: int = 2) -> None:
    for row in sheet.iter_rows(min_row=min_row):
        for cell in row:
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def build_report_xlsx(report: dict[str, Any]) -> bytes:
    """Build a small workbook with an overview, five scores, actions, and limited evidence."""
    compact = compact_report(report)
    workbook = Workbook()

    overview = workbook.active
    overview.title = "Overview"
    overview.append(["Result", "Details"])
    _style_header(overview)
    values = [
        ("Decision", compact["quality_gate"]),
        ("What this means", compact["what_this_means"]),
        ("Overall score", compact["overall_score_percent"]),
        ("Document", compact["document"]["title"]),
        ("Source DOCX", compact["document"]["source_docx"]),
        ("Chunk count", compact["document"]["chunk_count"]),
        ("Chunking strategy", compact["document"]["strategy"]),
        ("DOCX source coverage", compact["coverage"]["docx_source_coverage_percent"]),
        ("DOCX source units missing from chunks", compact["coverage"]["docx_units_not_in_chunks"]),
        ("Possible PDF-to-DOCX omissions", compact["coverage"]["possible_pdf_to_docx_omissions"]),
        ("Next steps", len(compact["next_steps"])),
        ("Evidence rows included", len(compact["evidence_to_review"])),
        ("Additional evidence rows omitted", compact["evidence_rows_omitted"]),
        ("Evaluation ID", compact["evaluation_id"]),
    ]
    for key, value in values:
        overview.append([key, _excel_safe(value)])
    overview.column_dimensions["A"].width = 38
    overview.column_dimensions["B"].width = 88
    _style_body(overview)
    for row in overview.iter_rows(min_row=2):
        row[0].fill = _SUBHEADER_FILL
        row[0].font = Font(bold=True)

    score_sheet = workbook.create_sheet("Scorecard")
    score_sheet.append(["Dimension", "Score %", "Result", "What this checks", "Failed", "Unresolved"])
    _style_header(score_sheet)
    for item in compact["scorecard"]:
        score_sheet.append([_excel_safe(item["dimension"]), item["score_percent"],
                            _excel_safe(item["result"]), _excel_safe(item["what_it_checks"]),
                            item["failed_checks"], item["unresolved_checks"]])
    score_sheet.freeze_panes = "A2"
    score_sheet.auto_filter.ref = score_sheet.dimensions
    for col, width in {"A": 32, "B": 14, "C": 18, "D": 76, "E": 12, "F": 14}.items():
        score_sheet.column_dimensions[col].width = width
    _style_body(score_sheet)

    steps_sheet = workbook.create_sheet("Next Steps")
    steps_sheet.append(["Priority", "Issue", "What to do", "Affected chunks", "Source refs", "Pages"])
    _style_header(steps_sheet)
    for item in compact["next_steps"]:
        steps_sheet.append([_excel_safe(item["priority"]), _excel_safe(item["issue"]),
                            _excel_safe(item["next_step"]), _excel_safe(item["chunk_ids"]),
                            _excel_safe(item["source_refs"]), _excel_safe(item["pages"])])
    if not compact["next_steps"]:
        steps_sheet.append(["", "No action needed", "No corrective actions were identified.", "", "", ""])
    steps_sheet.freeze_panes = "A2"
    steps_sheet.auto_filter.ref = steps_sheet.dimensions
    for col, width in {"A": 14, "B": 36, "C": 78, "D": 28, "E": 26, "F": 14}.items():
        steps_sheet.column_dimensions[col].width = width
    _style_body(steps_sheet)

    evidence_sheet = workbook.create_sheet("Evidence to Review")
    evidence_sheet.append(["Chunk", "Check", "Result", "Severity", "Why flagged", "Source refs",
                           "Chunk refs", "Source excerpt", "Chunk excerpt"])
    _style_header(evidence_sheet)
    for item in compact["evidence_to_review"]:
        evidence_sheet.append([_excel_safe(item["chunk_id"]), _excel_safe(item["criterion"]),
                               _excel_safe(item["outcome"]), _excel_safe(item["severity"]),
                               _excel_safe(item["explanation"]), _excel_safe(item["source_refs"]),
                               _excel_safe(item["chunk_refs"]), _excel_safe(item["source_excerpt"]),
                               _excel_safe(item["chunk_excerpt"])])
    if not compact["evidence_to_review"]:
        evidence_sheet.append(["", "No failed or inconclusive scorecard checks", "", "", "", "", "", "", ""])
    evidence_sheet.freeze_panes = "A2"
    evidence_sheet.auto_filter.ref = evidence_sheet.dimensions
    for col, width in {"A": 20, "B": 32, "C": 16, "D": 14, "E": 62, "F": 24,
                       "G": 24, "H": 42, "I": 42}.items():
        evidence_sheet.column_dimensions[col].width = width
    _style_body(evidence_sheet)
    for row in range(2, evidence_sheet.max_row + 1):
        outcome = evidence_sheet.cell(row=row, column=3).value
        if outcome in _OUTCOME_FILLS:
            evidence_sheet.cell(row=row, column=3).fill = _OUTCOME_FILLS[outcome]

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def report_json_bytes(report: dict[str, Any]) -> bytes:
    compact = compact_report(report)
    return (json.dumps(compact, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
