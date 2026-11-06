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
    "Needs a fix": PatternFill("solid", fgColor="FCE4D6"),
    "Check against source": PatternFill("solid", fgColor="FFF2CC"),
    "Could not evaluate": PatternFill("solid", fgColor="D9EAF7"),
    "Looks good": PatternFill("solid", fgColor="E2F0D9"),
    "Needs correction": PatternFill("solid", fgColor="FCE4D6"),
    "Needs source review": PatternFill("solid", fgColor="FFF2CC"),
    "Could not assess": PatternFill("solid", fgColor="FFF2CC"),
}
_MAX_EXCERPT_CHARS = 700


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


def build_review_queue(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only evidence-backed scored findings that need a human decision."""
    primary_ids = _primary_criterion_ids(report)
    criteria = {item.get("id"): item.get("name") for item in report.get("criteria", [])}
    chunk_order = {item.get("chunk_id"): index for index, item in enumerate(report.get("chunks", []))}
    unavailable_chunks = set(report.get("llm", {}).get("could_not_evaluate_chunk_ids", []))
    if not report.get("llm", {}).get("configured"):
        unavailable_chunks.update(chunk_order)
    rows = []
    for item in report.get("findings", []):
        if item.get("criterion_id") not in primary_ids or item.get("outcome") not in {"FAIL", "INCONCLUSIVE"}:
            continue
        failed = item["outcome"] == "FAIL"
        unavailable = item.get("chunk_id") in unavailable_chunks
        status = "Needs a fix" if failed else ("Could not evaluate" if unavailable else "Check against source")
        rows.append({
            "chunk_id": item.get("chunk_id") or "Document",
            "section": " > ".join(next((chunk.get("section_path", []) for chunk in report.get("chunks", [])
                                              if chunk.get("chunk_id") == item.get("chunk_id")), [])) or "No section path",
            "criterion": item.get("criterion") or criteria.get(item.get("criterion_id"), item.get("criterion_id")),
            "criterion_id": item.get("criterion_id"),
            "status": status,
            "reason": item.get("explanation") or "The judge did not provide a clear explanation.",
            "source_refs": item.get("source_evidence_refs") or [],
            "source_excerpt": item.get("source_excerpt") or "No aligned source excerpt was available.",
            "chunk_excerpt": item.get("chunk_excerpt") or "",
            "improvement": (
                "No chunk change is recommended because the judge did not complete this assessment. Fix the LiteLLM error and rerun."
                if unavailable else item.get("improvement") or (
                    "Compare the source and chunk before changing anything." if not failed else
                    "Correct only the mismatch demonstrated by the cited source."
                )
            ),
            "reviewer_action": "Fix the judge error and rerun" if unavailable else {
                "KEEP_AS_IS": "Keep as is", "EDIT_CHUNK_TEXT": "Edit chunk text",
                "MOVE_TO_SECTION": "Move to the correct section", "SPLIT_CHUNK": "Split this chunk",
                "ADJUST_BOUNDARY": "Adjust the chunk boundary", "ADD_CONTEXT": "Add necessary context",
                "CHECK_SOURCE": "Check the source passage", "CHECK_PDFLENS": "Review PDF-to-DOCX extraction",
                "RETRY_EVALUATION": "Fix the judge error and rerun",
            }.get(item.get("suggested_action"), "Check the cited evidence"),
            "sequence": chunk_order.get(item.get("chunk_id"), 10**9),
            "severity_rank": {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}.get(item.get("severity"), 4),
        })
    rows.sort(key=lambda item: (item["severity_rank"], item["sequence"], item["criterion"]))
    return rows


def _review_counts(report: dict[str, Any], queue: list[dict[str, Any]]) -> dict[str, int]:
    states: dict[str, set[str]] = {}
    for item in queue:
        states.setdefault(item["chunk_id"], set()).add(item["status"])
    chunks = report.get("chunks", [])
    correction = sum("Needs a fix" in states.get(item.get("chunk_id"), set()) for item in chunks)
    verification = sum("Check against source" in states.get(item.get("chunk_id"), set()) for item in chunks)
    unavailable = sum("Could not evaluate" in states.get(item.get("chunk_id"), set()) for item in chunks)
    clear = max(0, len(chunks) - len({chunk_id for chunk_id, labels in states.items()
                                      if labels & {"Needs a fix", "Check against source", "Could not evaluate"}}))
    if correction:
        decision, message = "Fixes needed", f"{correction} chunk(s) have an evidence-backed issue. Review those chunks first."
    elif unavailable:
        decision, message = "Could not evaluate", f"The judge did not complete {unavailable} chunk review(s). Fix the judge error and rerun."
    elif verification:
        decision, message = "Check against source", f"{verification} chunk(s) need a source check before they can be cleared."
    else:
        decision, message = "Looks good", "No clear chunking issue was found in the evaluated chunks."
    return {"decision": decision, "message": message, "total_chunks": len(chunks),
            "chunks_needing_correction": correction, "chunks_needing_source_review": verification,
            "chunks_not_evaluated": unavailable, "chunks_looks_good": clear,
            "chunks_with_no_flagged_issue": clear, "issues_to_review": len(queue)}


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Return a concise, evidence-backed report for normal download/API use."""
    summary = report.get("summary", {})
    metadata = report.get("chunk_file_metadata", {})
    review_queue = build_review_queue(report)
    review_counts = report.get("review_summary") or _review_counts(report, review_queue)
    source_docx = metadata.get("source_docx") or report.get("inputs", {}).get("source_docx_member")
    input_docx = report.get("inputs", {}).get("source_docx_member")
    scorecard = [
        {
            "question": item.get("dimension"),
            "result": {"PASS": "Looks good", "FAIL": "Needs a fix", "MIXED": "Some checks need a fix", "INCONCLUSIVE": "Check against source"}.get(item.get("status"), item.get("status")),
            "what_to_check": item.get("what_it_checks"),
            "looks_good": item.get("pass_count"),
            "needs_a_fix": item.get("fail_count"),
            "check_against_source": item.get("inconclusive_count"),
        }
        for item in report.get("scorecard", {}).get("dimensions", [])
    ]
    return {
        "report_version": "human-chunk-review-5",
        "evaluation_id": report.get("evaluation_id"),
        "created_at": report.get("created_at"),
        "quality_gate": report.get("quality_gate"),
        "quality_gate_label": review_counts.get("decision") or {"PASS": "Looks good", "FAIL": "Fixes needed", "REVIEW_REQUIRED": "Check against source", "NOT_EVALUATED": "Could not evaluate"}.get(report.get("quality_gate"), report.get("quality_gate")),
        "what_this_means": review_counts.get("message") or report.get("action_plan", {}).get("summary"),
        "review_summary": review_counts,
        "chunk_reviews": report.get("chunk_reviews", []),
        "document": {
            "title": metadata.get("document_title") or report.get("document_id"),
            "source_docx": Path(str(source_docx)).name if source_docx else input_docx,
            "chunk_file": report.get("inputs", {}).get("chunks_filename"),
            "chunk_count": summary.get("total_chunks"),
            "strategy": report.get("chunking_strategy"),
        },
        "score_explanation": {
            "meaning": "Only the five chunk review questions determine the rating. PDF-to-DOCX diagnostics are separate.",
            "rule": "Looks good = +1; needs a fix = -1 when source evidence proves it; check against source = 0 and is not a failure.",
            "rating_percent": summary.get("overall_score"),
        },
        "scorecard": scorecard,
        "review_queue": [{key: value for key, value in item.items()
                           if key not in {"sequence", "severity_rank"}} for item in review_queue],
        "coverage": {
            "note": "Diagnostic only. These PDF/DOCX coverage measurements do not contribute to the LLM criteria score.",
            "docx_source_coverage_percent": summary.get("source_coverage_percentage"),
            "covered_source_units": summary.get("covered_source_units"),
            "eligible_source_units": summary.get("eligible_source_units"),
            "docx_units_not_in_chunks": summary.get("missing_source_units"),
            "possible_pdf_to_docx_omissions": summary.get("pdf_docx_discrepancies"),
        },
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
    """Build a workbook with the criterion score, plain-language result, actions, and evidence."""
    compact = compact_report(report)
    workbook = Workbook()

    overview = workbook.active
    overview.title = "Overview"
    overview.append(["Result", "Details"])
    _style_header(overview)
    values = [
        ("Decision", compact["quality_gate_label"]),
        ("What this means", compact["what_this_means"]),
        ("Chunks evaluated", compact["review_summary"]["total_chunks"]),
        ("Chunks with evidence-backed issues", compact["review_summary"]["chunks_needing_correction"]),
        ("Chunks to verify against source", compact["review_summary"]["chunks_needing_source_review"]),
        ("Chunks with no flagged issue", compact["review_summary"]["chunks_with_no_flagged_issue"]),
        ("Chunks not evaluated", compact["review_summary"]["chunks_not_evaluated"]),
        ("Issues in review queue", compact["review_summary"]["issues_to_review"]),
        ("Document", compact["document"]["title"]),
        ("Source DOCX", compact["document"]["source_docx"]),
        ("Chunk count", compact["document"]["chunk_count"]),
        ("Chunking strategy", compact["document"]["strategy"]),
    ]
    for key, value in values:
        overview.append([key, _excel_safe(value)])
    overview.column_dimensions["A"].width = 38
    overview.column_dimensions["B"].width = 88
    _style_body(overview)
    for row in overview.iter_rows(min_row=2):
        row[0].fill = _SUBHEADER_FILL
        row[0].font = Font(bold=True)

    chunk_sheet = workbook.create_sheet("Chunk Review", 1)
    chunk_sheet.append(["Verdict", "Chunk", "Section", "Summary", "What works",
                        "Problems", "Suggested improvements", "Recommended action", "Chunk text"])
    _style_header(chunk_sheet)
    for item in compact["chunk_reviews"]:
        path = " > ".join(item.get("section_path", [])) or "No section path"
        strengths = "\n".join(
            f"{row['criterion']}: {row['explanation']} (source refs: {', '.join(row.get('source_refs') or [])})\n"
            f"Source: {row.get('source_excerpt', '')}\nChunk: {row.get('chunk_excerpt', '')}"
            for row in item.get("strengths", [])
        ) or "No specific strength was confirmed."
        problems = "\n".join(f"{row['criterion']}: {row['explanation']}" for row in item.get("problems", [])) or "No confirmed problem."
        improvements = "\n".join(item.get("improvements", [])) or "Check cited source evidence."
        action_label = {
            "KEEP_AS_IS": "Keep as is", "EDIT_CHUNK_TEXT": "Edit chunk text",
            "MOVE_TO_SECTION": "Move to the correct section", "SPLIT_CHUNK": "Split this chunk",
            "ADJUST_BOUNDARY": "Adjust the chunk boundary", "ADD_CONTEXT": "Add necessary context",
            "CHECK_SOURCE": "Check the source passage", "CHECK_PDFLENS": "Review PDF-to-DOCX extraction",
            "RETRY_EVALUATION": "Fix the judge error and rerun",
        }.get(item.get("recommended_action"), "Check the evidence")
        chunk_sheet.append([_excel_safe(item.get("verdict")), _excel_safe(item.get("chunk_id")),
                            _excel_safe(path), _excel_safe(item.get("summary")), _excel_safe(strengths),
                            _excel_safe(problems), _excel_safe(improvements), action_label,
                            _excel_safe(_short(item.get("chunk_text"), 3000))])
    chunk_sheet.freeze_panes = "A2"
    chunk_sheet.auto_filter.ref = chunk_sheet.dimensions
    for col, width in {"A": 24, "B": 22, "C": 44, "D": 64, "E": 64, "F": 64,
                       "G": 64, "H": 32, "I": 72}.items():
        chunk_sheet.column_dimensions[col].width = width
    _style_body(chunk_sheet)
    for row in range(2, chunk_sheet.max_row + 1):
        status = chunk_sheet.cell(row=row, column=1).value
        fill = {"Needs a fix": "FCE4D6", "Check against source": "FFF2CC", "Looks good": "E2F0D9"}.get(status)
        if fill:
            chunk_sheet.cell(row=row, column=1).fill = PatternFill("solid", fgColor=fill)

    queue_sheet = workbook.create_sheet("Evidence & Actions", 2)
    queue_sheet.append(["Reviewer result", "Chunk", "Section", "Criterion", "Why it was flagged",
                        "Source reference", "Source passage", "Chunk text", "Suggested improvement", "Action"])
    _style_header(queue_sheet)
    for item in compact["review_queue"]:
        queue_sheet.append([
            _excel_safe(item["status"]), _excel_safe(item["chunk_id"]), _excel_safe(item["section"]),
            _excel_safe(item["criterion"]), _excel_safe(item["reason"]),
            _excel_safe(item["source_refs"]), _excel_safe(item["source_excerpt"]),
            _excel_safe(item["chunk_excerpt"]), _excel_safe(item["improvement"]), _excel_safe(item["reviewer_action"]),
        ])
    if not compact["review_queue"]:
        queue_sheet.append(["No issue found", "", "", "", "No failed or unresolved scored checks need review."])
    queue_sheet.freeze_panes = "A2"
    queue_sheet.auto_filter.ref = queue_sheet.dimensions
    for col, width in {"A": 24, "B": 22, "C": 42, "D": 34, "E": 58,
                       "F": 30, "G": 52, "H": 52, "I": 60, "J": 32}.items():
        queue_sheet.column_dimensions[col].width = width
    _style_body(queue_sheet)
    for row in range(2, queue_sheet.max_row + 1):
        status = queue_sheet.cell(row=row, column=1).value
        if status in _OUTCOME_FILLS:
            queue_sheet.cell(row=row, column=1).fill = _OUTCOME_FILLS[status]

    score_sheet = workbook.create_sheet("Five Questions")
    score_sheet.append(["Question for the reviewer", "Result", "Looks good", "Needs a fix", "Check against source", "What this asks"])
    _style_header(score_sheet)
    for item in compact["scorecard"]:
        score_sheet.append([_excel_safe(item["question"]),
                            _excel_safe(item["result"]), item["looks_good"], item["needs_a_fix"],
                            item["check_against_source"], _excel_safe(item["what_to_check"])])
    score_sheet.freeze_panes = "A2"
    score_sheet.auto_filter.ref = score_sheet.dimensions
    for col, width in {"A": 40, "B": 26, "C": 16, "D": 16, "E": 24, "F": 76}.items():
        score_sheet.column_dimensions[col].width = width
    _style_body(score_sheet)

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def report_json_bytes(report: dict[str, Any]) -> bytes:
    compact = compact_report(report)
    action_labels = {
        "KEEP_AS_IS": "Keep as is", "EDIT_CHUNK_TEXT": "Edit chunk text",
        "MOVE_TO_SECTION": "Move to the correct section", "SPLIT_CHUNK": "Split this chunk",
        "ADJUST_BOUNDARY": "Adjust the chunk boundary", "ADD_CONTEXT": "Add necessary context",
        "CHECK_SOURCE": "Check the source passage", "CHECK_PDFLENS": "Review PDF-to-DOCX extraction",
        "RETRY_EVALUATION": "Fix the judge error and rerun",
    }
    chunk_reviews = []
    for review in compact["chunk_reviews"]:
        row = dict(review)
        row["recommended_action"] = action_labels.get(row.get("recommended_action"), "Check the evidence")
        chunk_reviews.append(row)
    human_report = {
        "report_version": compact["report_version"],
        "evaluation_id": compact["evaluation_id"],
        "created_at": compact["created_at"],
        "document": compact["document"],
        "decision": {
            "verdict": compact["quality_gate_label"],
            "summary": compact["what_this_means"],
        },
        "review_summary": compact["review_summary"],
        "chunk_reviews": chunk_reviews,
        "five_review_questions": compact["scorecard"],
        "scoring_details": compact["score_explanation"],
        "source_diagnostics": compact["coverage"],
        "judge": compact["judge"],
    }
    return (json.dumps(human_report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
