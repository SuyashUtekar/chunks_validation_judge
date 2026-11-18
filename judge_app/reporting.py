from __future__ import annotations

import json
import math
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
        "report_version": "human-chunk-review-6",
        "evaluation_id": report.get("evaluation_id"),
        "created_at": report.get("created_at"),
        "quality_gate": report.get("quality_gate"),
        "quality_gate_label": review_counts.get("decision") or {"PASS": "Looks good", "FAIL": "Fixes needed", "REVIEW_REQUIRED": "Check against source", "NOT_EVALUATED": "Could not evaluate"}.get(report.get("quality_gate"), report.get("quality_gate")),
        "what_this_means": review_counts.get("message") or report.get("action_plan", {}).get("summary"),
        "review_summary": review_counts,
        "chunk_reviews": report.get("chunk_reviews", []),
        "visual_reviews": report.get("visual_reviews", []),
        "visual_review_summary": report.get("visual_review_summary", {}),
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


def _build_legacy_report_xlsx(report: dict[str, Any]) -> bytes:
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
        ("Rating", f"{compact['score_explanation']['rating_percent']}%"
                    if compact["score_explanation"]["rating_percent"] is not None else "Not available"),
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

    criteria = [
        item for item in report.get("criteria", [])
        if item.get("method") == "llm" and item.get("scope") == "chunk"
    ]
    criteria_sheet = workbook.create_sheet("Chunk Criteria Review", 2)
    headers = ["Chunk ID", "Section"]
    for criterion in criteria:
        name = str(criterion.get("name") or criterion.get("id") or "Criterion")
        headers.extend([f"{name} — score (0–1)", f"{name} — review"])
    headers.extend(["Final LLM verdict", "Severity", "Human review"])
    criteria_sheet.append([
        "Score guide: 1 = Looks good; 0 = Needs a fix; blank = not assessed or not applicable. "
        "Human review is intentionally left empty."
    ])
    criteria_sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    guide = criteria_sheet.cell(row=1, column=1)
    guide.fill = _SUBHEADER_FILL
    guide.font = Font(name="Arial", size=10, italic=True, color="404040")
    guide.alignment = Alignment(vertical="center", wrap_text=True)
    criteria_sheet.row_dimensions[1].height = 30
    criteria_sheet.append(headers)
    _style_header(criteria_sheet, row_number=2)
    criteria_sheet.row_dimensions[2].height = 54

    criterion_findings = {
        (item.get("chunk_id"), item.get("criterion_id")): item
        for item in report.get("findings", [])
        if item.get("method") == "llm"
    }
    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    score_value = {"PASS": 1.0, "FAIL": 0.0}
    for review in compact["chunk_reviews"]:
        values = [
            _excel_safe(review.get("chunk_id")),
            _excel_safe(" > ".join(review.get("section_path", [])) or "No section path"),
        ]
        failed_severities = []
        for criterion in criteria:
            finding = criterion_findings.get((review.get("chunk_id"), criterion.get("id")), {})
            outcome = finding.get("outcome")
            score = score_value.get(outcome, "")
            explanation = _short(finding.get("explanation") or "No criterion assessment was returned.", 260)
            outcome_label = {
                "PASS": "Looks good", "FAIL": "Needs a fix",
                "INCONCLUSIVE": "Check against source", "N/A": "Not applicable",
            }.get(outcome, "Could not evaluate")
            improvement = finding.get("improvement")
            criterion_review = f"{outcome_label}: {explanation}"
            if improvement and outcome in {"FAIL", "INCONCLUSIVE"}:
                criterion_review += f" Suggested improvement: {_short(improvement, 180)}"
            values.extend([score, _excel_safe(criterion_review)])
            if outcome == "FAIL" and finding.get("severity") in severity_rank:
                failed_severities.append(finding["severity"])

        verdict_parts = [
            f"{review.get('verdict', 'Could not evaluate')}: "
            f"{_short(review.get('summary'), 420)}"
        ]
        for label, field_name in (("Strength", "strengths"), ("Problem", "problems"),
                                  ("Verify", "checks_to_verify")):
            for detail in review.get(field_name, [])[:2]:
                verdict_parts.append(
                    f"{label} — {detail.get('criterion', 'Review')}: "
                    f"{_short(detail.get('explanation'), 220)}"
                )
        if review.get("improvements"):
            verdict_parts.append(f"Next step: {_short(review['improvements'][0], 240)}")
        if failed_severities:
            severity = min(failed_severities, key=severity_rank.get)
        elif review.get("verdict") == "Looks good":
            severity = "None"
        elif review.get("verdict") == "Needs a fix":
            severity = "Not specified"
        else:
            severity = "Not assessed"
        values.extend([
            _excel_safe("\n".join(verdict_parts)),
            severity,
            "",  # Reserved for a human reviewer to fill in.
        ])
        criteria_sheet.append(values)

    criteria_sheet.freeze_panes = "C3"
    criteria_sheet.auto_filter.ref = f"A2:{criteria_sheet.cell(row=2, column=len(headers)).column_letter}{criteria_sheet.max_row}"
    criteria_sheet.column_dimensions["A"].width = 22
    criteria_sheet.column_dimensions["B"].width = 42
    for index in range(3, 3 + len(criteria) * 2, 2):
        criteria_sheet.column_dimensions[criteria_sheet.cell(row=1, column=index).column_letter].width = 14
        criteria_sheet.column_dimensions[criteria_sheet.cell(row=1, column=index + 1).column_letter].width = 48
    final_column = 3 + len(criteria) * 2
    criteria_sheet.column_dimensions[criteria_sheet.cell(row=1, column=final_column).column_letter].width = 76
    criteria_sheet.column_dimensions[criteria_sheet.cell(row=1, column=final_column + 1).column_letter].width = 18
    criteria_sheet.column_dimensions[criteria_sheet.cell(row=1, column=final_column + 2).column_letter].width = 28
    _style_body(criteria_sheet, min_row=3)
    for row_number in range(3, criteria_sheet.max_row + 1):
        estimated_lines = 1
        for value in criteria_sheet[row_number]:
            width = criteria_sheet.column_dimensions[value.column_letter].width or 18
            for line in str(value.value or "").splitlines() or [""]:
                estimated_lines = max(estimated_lines, math.ceil(len(line) / max(8, width)))
        criteria_sheet.row_dimensions[row_number].height = min(360, max(54, estimated_lines * 15 + 8))
        verdict_cell = criteria_sheet.cell(row=row_number, column=final_column)
        verdict_label = str(verdict_cell.value or "").split(":", 1)[0]
        verdict_fill = {
            "Needs a fix": "FCE4D6", "Check against source": "FFF2CC",
            "Looks good": "E2F0D9", "Could not evaluate": "D9EAF7",
        }.get(verdict_label)
        if verdict_fill:
            verdict_cell.fill = PatternFill("solid", fgColor=verdict_fill)
        severity_cell = criteria_sheet.cell(row=row_number, column=final_column + 1)
        severity_fill = {
            "CRITICAL": "F4CCCC", "HIGH": "FCE4D6",
            "MEDIUM": "FFF2CC", "LOW": "D9EAF7",
        }.get(severity_cell.value)
        if severity_fill:
            severity_cell.fill = PatternFill("solid", fgColor=severity_fill)
        for index in range(3, 3 + len(criteria) * 2, 2):
            score_cell = criteria_sheet.cell(row=row_number, column=index)
            score_cell.number_format = "0.00"
            score_cell.alignment = Alignment(horizontal="center", vertical="top")
            outcome = criterion_findings.get(
                (criteria_sheet.cell(row=row_number, column=1).value,
                 criteria[index // 2 - 1].get("id")),
                {},
            ).get("outcome")
            fill = {"PASS": "E2F0D9", "FAIL": "FCE4D6", "INCONCLUSIVE": "FFF2CC"}.get(outcome)
            if fill:
                criteria_sheet.cell(row=row_number, column=index + 1).fill = PatternFill("solid", fgColor=fill)
        criteria_sheet.cell(row=row_number, column=final_column + 2).fill = PatternFill(
            "solid", fgColor="FFF2CC"
        )

    queue_sheet = workbook.create_sheet("Evidence & Actions", 3)
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


def build_report_xlsx(report: dict[str, Any]) -> bytes:
    """Build a compact workbook with summary, criterion evidence, and visual review."""
    compact = compact_report(report)
    workbook = Workbook()
    reviews = compact["chunk_reviews"]
    criteria = [item for item in report.get("criteria", [])
                if item.get("method") == "llm" and item.get("scope") == "chunk"]
    primary_ids = {item.get("id") for item in criteria}
    findings = {(item.get("chunk_id"), item.get("criterion_id")): item
                for item in report.get("findings", []) if item.get("method") == "llm"}
    visual = compact["visual_reviews"]
    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    action_labels = {
        "KEEP_AS_IS": "Keep as is", "EDIT_CHUNK_TEXT": "Edit chunk text",
        "MOVE_TO_SECTION": "Move to the correct section", "SPLIT_CHUNK": "Split this chunk",
        "ADJUST_BOUNDARY": "Adjust the chunk boundary", "ADD_CONTEXT": "Add necessary context",
        "CHECK_SOURCE": "Check the source passage", "CHECK_PDFLENS": "Review PDF-to-DOCX extraction",
        "RETRY_EVALUATION": "Fix judge error and rerun",
    }

    overview = workbook.active
    overview.title = "Overview"
    overview.append(["Result", "Details"])
    _style_header(overview)
    summary = compact["review_summary"]
    visual_states = [item.get("status") for item in visual]
    rows = [
        ("Decision", compact["quality_gate_label"]),
        ("What this means", compact["what_this_means"]),
        ("Text criteria rating", f"{compact['score_explanation']['rating_percent']}%"
         if compact["score_explanation"]["rating_percent"] is not None else "Not available"),
        ("Chunks reviewed", summary["total_chunks"]),
        ("Chunks needing a fix", summary["chunks_needing_correction"]),
        ("Chunks to verify against source", summary["chunks_needing_source_review"]),
        ("Chunks with no flagged issue", summary["chunks_with_no_flagged_issue"]),
        ("Chunks not evaluated", summary["chunks_not_evaluated"]),
        ("Visual reviews completed", visual_states.count("Reviewed")),
        ("Visual reviews not selected", visual_states.count("Not selected")),
        ("Visual reviews needing manual review", visual_states.count("Not reviewed")),
        ("Score is based on", "The five text-based chunking criteria only"),
        ("Visual review impact", "Advisory only; visual findings do not change the score"),
        ("Score guide", "1 = Looks good; 0 = Needs a fix; blank = unresolved or not applicable"),
        ("Document", compact["document"]["title"]),
        ("Source DOCX", compact["document"]["source_docx"]),
        ("Chunking strategy", compact["document"]["strategy"]),
        ("Evaluation ID", compact["evaluation_id"]),
    ]
    for label, value in rows:
        overview.append([label, _excel_safe(value)])
    overview.column_dimensions["A"].width = 38
    overview.column_dimensions["B"].width = 96
    _style_body(overview)
    for row in overview.iter_rows(min_row=2):
        row[0].fill = _SUBHEADER_FILL
        row[0].font = Font(bold=True)
    overview.freeze_panes = "A2"

    summary_sheet = workbook.create_sheet("Chunk Summary")
    summary_sheet.append(["Chunk ID", "Section", "Checks passed", "Checks failed",
                         "Checks to verify", "Checks unavailable", "LLM verdict", "What the judge found",
                         "What works", "Problem", "Suggested improvement", "Severity",
                         "Suggested action", "Human review"])
    _style_header(summary_sheet)
    for review in reviews:
        chunk_id = review.get("chunk_id")
        chunk_findings = [findings.get((chunk_id, criterion_id), {}) for criterion_id in primary_ids]
        passed = sum(item.get("outcome") == "PASS" for item in chunk_findings)
        failed = sum(item.get("outcome") == "FAIL" for item in chunk_findings)
        call_unavailable = review.get("verdict") == "Could not evaluate"
        source_check = sum(item.get("outcome") == "INCONCLUSIVE" for item in chunk_findings) if not call_unavailable else 0
        unavailable = sum(item.get("outcome") == "INCONCLUSIVE" for item in chunk_findings) if call_unavailable else 0
        severities = [item.get("severity") for item in chunk_findings
                      if item.get("outcome") == "FAIL" and item.get("severity") in severity_rank]
        severity = min(severities, key=severity_rank.get) if severities else (
            "None" if review.get("verdict") == "Looks good" else "Not assessed")
        strengths = "; ".join(f"{item.get('criterion')}: {item.get('explanation')}"
                               for item in review.get("strengths", [])[:2])
        problems = "; ".join(f"{item.get('criterion')}: {item.get('explanation')}"
                              for item in review.get("problems", [])[:3])
        improvements = "; ".join(review.get("improvements", [])[:2])
        summary_sheet.append([
            _excel_safe(chunk_id), _excel_safe(" > ".join(review.get("section_path") or []) or "No section path"),
            passed, failed, source_check, unavailable,
            _excel_safe(review.get("verdict")), _excel_safe(_short(review.get("summary"), 650)),
            _excel_safe(_short(strengths, 700)), _excel_safe(_short(problems, 1000)),
            _excel_safe(_short(improvements, 700)), severity,
            action_labels.get(review.get("recommended_action"), "Review evidence"), "",
        ])
    summary_sheet.freeze_panes = "A2"
    summary_sheet.auto_filter.ref = summary_sheet.dimensions
    for col, width in {"A": 20, "B": 42, "C": 14, "D": 14, "E": 18, "F": 13,
                       "G": 22, "H": 62, "I": 62, "J": 68, "K": 64, "L": 16,
                       "M": 32, "N": 28}.items():
        summary_sheet.column_dimensions[col].width = width
    _style_body(summary_sheet)
    for row in range(2, summary_sheet.max_row + 1):
        verdict = summary_sheet.cell(row, 7).value
        if verdict in _OUTCOME_FILLS:
            summary_sheet.cell(row, 7).fill = _OUTCOME_FILLS[verdict]
        summary_sheet.cell(row, 14).fill = PatternFill("solid", fgColor="FFF2CC")

    evidence = workbook.create_sheet("Criterion Evidence")
    evidence.append(["Chunk ID", "Section", "Criterion", "Score (0–1)", "Result", "Why this result",
                     "Source reference", "Source passage", "Chunk passage", "Suggested improvement", "Severity"])
    _style_header(evidence)
    labels = {"PASS": "Looks good", "FAIL": "Needs a fix", "INCONCLUSIVE": "Check against source", "N/A": "Not applicable"}
    for review in reviews:
        chunk_id = review.get("chunk_id")
        section = " > ".join(review.get("section_path") or []) or "No section path"
        for criterion in criteria:
            finding = findings.get((chunk_id, criterion.get("id")), {})
            outcome = finding.get("outcome")
            evidence.append([
                _excel_safe(chunk_id), _excel_safe(section),
                _excel_safe(criterion.get("name") or criterion.get("id")),
                {"PASS": 1, "FAIL": 0}.get(outcome, ""), labels.get(outcome, "Could not evaluate"),
                _excel_safe(_short(finding.get("explanation") or "No criterion result was returned.", 800)),
                _excel_safe(finding.get("source_evidence_refs") or []),
                _excel_safe(_short(finding.get("source_excerpt"), 1400)),
                _excel_safe(_short(finding.get("chunk_excerpt"), 1400)),
                _excel_safe(_short(finding.get("improvement"), 700)), finding.get("severity") or "",
            ])
    evidence.freeze_panes = "A2"
    evidence.auto_filter.ref = evidence.dimensions
    for col, width in {"A": 20, "B": 42, "C": 38, "D": 14, "E": 24, "F": 62,
                       "G": 30, "H": 72, "I": 72, "J": 62, "K": 16}.items():
        evidence.column_dimensions[col].width = width
    _style_body(evidence)
    for row in range(2, evidence.max_row + 1):
        evidence.cell(row, 4).number_format = "0"
        evidence.cell(row, 4).alignment = Alignment(horizontal="center", vertical="top")
        fill = {"Looks good": "E2F0D9", "Needs a fix": "FCE4D6",
                "Check against source": "FFF2CC", "Could not evaluate": "D9EAF7"}.get(evidence.cell(row, 5).value)
        if fill:
            evidence.cell(row, 5).fill = PatternFill("solid", fgColor=fill)

    visual_sheet = workbook.create_sheet("Visual Review")
    visual_sheet.append(["Chunk ID", "Section", "Review status", "Why selected / not selected", "PDF page(s)",
                         "Visual observation", "Comparison with chunk", "Visual verdict", "Confidence",
                         "Severity", "Suggested improvement", "Human review"])
    _style_header(visual_sheet)
    for item in visual:
        visual_sheet.append([
            _excel_safe(item.get("chunk_id")),
            _excel_safe(" > ".join(item.get("section_path") or []) or "No section path"),
            _excel_safe(item.get("status")), _excel_safe(item.get("reason")),
            _excel_safe(item.get("page_numbers") or []),
            _excel_safe(_short(item.get("visual_observation"), 1200)),
            _excel_safe(_short(item.get("chunk_comparison"), 1200)),
            _excel_safe(item.get("verdict")), item.get("confidence"),
            _excel_safe(item.get("severity") or ""), _excel_safe(_short(item.get("improvement"), 800)), "",
        ])
    if not visual:
        visual_sheet.append(["No visual review data was produced for this evaluation."])
    visual_sheet.freeze_panes = "A2"
    visual_sheet.auto_filter.ref = visual_sheet.dimensions
    for col, width in {"A": 20, "B": 42, "C": 20, "D": 62, "E": 16, "F": 68,
                       "G": 68, "H": 24, "I": 14, "J": 16, "K": 62, "L": 28}.items():
        visual_sheet.column_dimensions[col].width = width
    _style_body(visual_sheet)
    for row in range(2, visual_sheet.max_row + 1):
        visual_sheet.cell(row, 12).fill = PatternFill("solid", fgColor="FFF2CC")
        visual_sheet.cell(row, 9).number_format = "0%"

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
        "visual_reviews": compact["visual_reviews"],
        "visual_review_summary": compact["visual_review_summary"],
        "five_review_questions": compact["scorecard"],
        "scoring_details": compact["score_explanation"],
        "source_diagnostics": compact["coverage"],
        "judge": compact["judge"],
    }
    return (json.dumps(human_report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
