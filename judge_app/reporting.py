from __future__ import annotations

import json
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_SUBHEADER_FILL = PatternFill("solid", fgColor="D9EAF7")
_OUTCOME_FILLS = {
    "PASS": PatternFill("solid", fgColor="E2F0D9"),
    "FAIL": PatternFill("solid", fgColor="FCE4D6"),
    "INCONCLUSIVE": PatternFill("solid", fgColor="FFF2CC"),
    "N/A": PatternFill("solid", fgColor="E7E6E6"),
}


def _excel_safe(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (list, dict, tuple)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def _append_table(sheet: Any, headers: list[str], rows: list[list[Any]]) -> None:
    sheet.append(headers)
    for cell in sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        sheet.append([_excel_safe(value) for value in row])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions if rows else f"A1:{get_column_letter(len(headers))}1"
    sheet.row_dimensions[1].height = 30
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for column in range(1, len(headers) + 1):
        width = min(60, max(14, max((len(str(sheet.cell(row=row, column=column).value or ""))
                                    for row in range(1, min(sheet.max_row, 50) + 1)), default=14) + 2))
        sheet.column_dimensions[get_column_letter(column)].width = width


def build_report_xlsx(report: dict[str, Any]) -> bytes:
    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "Summary"
    summary = report["summary"]
    top_rows = [
        ["Evaluation ID", report["evaluation_id"]],
        ["Status", report["status"]],
        ["Quality gate", report["quality_gate"]],
        ["What this means", report.get("action_plan", {}).get("summary")],
        ["Overall score (average of scored dimensions)", summary.get("overall_score")],
        ["Scored dimensions", summary.get("scored_dimensions")],
        ["Primary scorecard failures", summary.get("core_failures")],
        ["Primary scorecard unresolved checks", summary.get("core_inconclusive")],
        ["Critical failures", summary["critical_failures"]],
        ["Total chunks", summary["total_chunks"]],
        ["Chunks failing scorecard", summary["chunks_failed"]],
        ["Chunks requiring review", summary["chunks_with_review_required"]],
        ["DOCX source coverage %", summary["source_coverage_percentage"]],
        ["Covered / eligible DOCX source units", f"{summary['covered_source_units']} / {summary['eligible_source_units']}"],
        ["PDF-to-DOCX discrepancies to review", summary["pdf_docx_discrepancies"]],
        ["Pass candidate threshold", report["quality_gate_thresholds"]["pass_candidate_at"]],
        ["Review boundary", report["quality_gate_thresholds"]["review_required_below"]],
        ["LLM configured", report["llm"]["configured"]],
        ["LLM model", report["llm"].get("model")],
        ["Document ID", report.get("document_id")],
        ["Chunking job ID", report.get("chunking_job_id")],
        ["Chunking strategy", report.get("chunking_strategy")],
        ["Criteria version", report["criteria_version"]],
        ["Application version", report["judge_application_version"]],
        ["Created at (UTC)", report["created_at"]],
    ]
    summary_sheet.append(["Field", "Value"])
    for cell in summary_sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = Font(bold=True, color="FFFFFF")
    for row in top_rows:
        summary_sheet.append([_excel_safe(value) for value in row])
    summary_sheet.column_dimensions["A"].width = 38
    summary_sheet.column_dimensions["B"].width = 80
    for row in summary_sheet.iter_rows(min_row=2):
        row[0].font = Font(bold=True)
        row[0].fill = _SUBHEADER_FILL
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    action_sheet = workbook.create_sheet("Action Plan", 1)
    action_plan = report.get("action_plan", {})
    action_sheet.append(["Overall interpretation", action_plan.get("summary", "No action summary available.")])
    action_sheet.merge_cells(start_row=1, start_column=2, end_row=1, end_column=8)
    action_sheet["A1"].fill = _SUBHEADER_FILL
    action_sheet["A1"].font = Font(bold=True)
    action_sheet["A1"].alignment = Alignment(vertical="top", wrap_text=True)
    action_sheet["B1"].alignment = Alignment(vertical="top", wrap_text=True)
    action_sheet.append([])
    action_headers = ["Priority", "Issue to review", "What to do", "Criteria", "Affected chunks", "Source refs", "Pages", "Finding IDs"]
    action_sheet.append(action_headers)
    for cell in action_sheet[3]:
        cell.fill = _HEADER_FILL
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for item in action_plan.get("actions", []):
        action_sheet.append([_excel_safe(item.get("priority")), _excel_safe(item.get("title")),
                             _excel_safe(item.get("what_to_do")), _excel_safe(item.get("criterion_ids")),
                             _excel_safe(item.get("chunk_ids")), _excel_safe(item.get("source_refs")),
                             _excel_safe(item.get("pages")), _excel_safe(item.get("finding_ids"))])
    if not action_plan.get("actions"):
        action_sheet.append(["", "No corrective actions were identified."])
    action_sheet.freeze_panes = "A4"
    action_sheet.auto_filter.ref = action_sheet.dimensions
    action_sheet.column_dimensions["A"].width = 14
    action_sheet.column_dimensions["B"].width = 36
    action_sheet.column_dimensions["C"].width = 90
    action_sheet.column_dimensions["D"].width = 18
    action_sheet.column_dimensions["E"].width = 36
    action_sheet.column_dimensions["F"].width = 34
    action_sheet.column_dimensions["G"].width = 18
    action_sheet.column_dimensions["H"].width = 28
    for row in action_sheet.iter_rows(min_row=4):
        for cell in row:
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    scorecard_sheet = workbook.create_sheet("Scorecard", 1)
    scorecard = report.get("scorecard", {})
    scorecard_sheet.append(["Overall score", scorecard.get("overall_score")])
    scorecard_sheet.append(["Scored dimensions", scorecard.get("scored_dimensions")])
    scorecard_sheet.append([])
    scorecard_headers = ["Dimension", "Score", "Result", "What this checks", "Supporting criterion IDs"]
    scorecard_sheet.append(scorecard_headers)
    for cell in scorecard_sheet[4]:
        cell.fill = _HEADER_FILL
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for item in scorecard.get("dimensions", []):
        scorecard_sheet.append([_excel_safe(item.get("dimension")), item.get("pass_percentage"),
                                _excel_safe(item.get("status")), _excel_safe(item.get("what_it_checks")),
                                _excel_safe(item.get("criteria"))])
    scorecard_sheet.freeze_panes = "A5"
    scorecard_sheet.auto_filter.ref = scorecard_sheet.dimensions
    scorecard_sheet.column_dimensions["A"].width = 34
    scorecard_sheet.column_dimensions["B"].width = 14
    scorecard_sheet.column_dimensions["C"].width = 18
    scorecard_sheet.column_dimensions["D"].width = 78
    scorecard_sheet.column_dimensions["E"].width = 28

    chunk_sheet = workbook.create_sheet("Chunk Results")
    chunk_headers = ["chunk_id", "sequence", "section_id", "section_path", "page_numbers",
                     "words", "characters", "source_alignment_confidence", "aligned_source_refs",
                     "aligned_pages", "unsupported_token_ratio", "overall_score", "scored_dimensions",
                     "detailed_PASS", "detailed_FAIL", "detailed_N/A", "detailed_INCONCLUSIVE",
                     "detailed_scorable", "detailed_pass_percentage", "signed_score", "raw_score",
                     "quality_gate", "chunk_excerpt"]
    chunk_rows = [[item.get("chunk_id"), item.get("sequence_number"), item.get("section_id"),
                   item.get("section_path"), item.get("page_numbers"), item.get("word_count"),
                   item.get("character_count"), item.get("alignment_confidence"),
                   item.get("aligned_source_refs"), item.get("aligned_pages"),
                   item.get("unsupported_token_ratio"), item.get("overall_score"), item.get("scored_dimensions"),
                   item.get("pass_count"), item.get("fail_count"), item.get("not_applicable_count"),
                   item.get("inconclusive_count"), item.get("scorable_count"), item.get("pass_percentage"),
                   item.get("signed_score"),
                   item.get("raw_score"), item.get("quality_gate"), item.get("chunk_excerpt")]
                  for item in report["chunks"]]
    _append_table(chunk_sheet, chunk_headers, chunk_rows)
    for row in range(2, chunk_sheet.max_row + 1):
        gate = chunk_sheet.cell(row=row, column=22).value
        if gate in _OUTCOME_FILLS:
            chunk_sheet.cell(row=row, column=22).fill = _OUTCOME_FILLS[gate]

    finding_sheet = workbook.create_sheet("Findings")
    finding_headers = ["finding_id", "scope", "chunk_id", "sequence", "criterion_id", "category",
                       "criterion", "outcome", "severity", "explanation", "confidence",
                       "human_review_required", "method", "source_evidence_refs", "chunk_evidence_refs",
                       "source_excerpt", "chunk_excerpt"]
    finding_rows = [[item.get(key) for key in [
        "finding_id", "scope", "chunk_id", "sequence_number", "criterion_id", "category",
        "criterion", "outcome", "severity", "explanation", "confidence",
        "human_review_required", "method", "source_evidence_refs", "chunk_evidence_refs",
        "source_excerpt", "chunk_excerpt",
    ]] for item in report["findings"]]
    _append_table(finding_sheet, finding_headers, finding_rows)
    for row in range(2, finding_sheet.max_row + 1):
        outcome = finding_sheet.cell(row=row, column=8).value
        if outcome in _OUTCOME_FILLS:
            finding_sheet.cell(row=row, column=8).fill = _OUTCOME_FILLS[outcome]

    sections_sheet = workbook.create_sheet("Sections")
    section_headers = ["section_id", "section_title", "section_path", "chunk_count",
                       "covered_source_units", "source_unit_count", "source_coverage_percentage",
                       "failed_chunks", "PASS", "FAIL", "N/A", "INCONCLUSIVE", "scorable",
                       "pass_percentage", "signed_score", "raw_score"]
    section_rows = [[item.get("section_id"), item.get("section_title"), item.get("section_path"),
                    item.get("chunk_count"), item.get("covered_source_units"),
                    item.get("source_unit_count"),
                    (100 * item["covered_source_units"] / item["source_unit_count"]
                     if item.get("source_unit_count") else None), item.get("failed_chunks"),
                    item.get("pass_count"), item.get("fail_count"), item.get("not_applicable_count"),
                    item.get("inconclusive_count"), item.get("scorable_count"),
                    item.get("pass_percentage"), item.get("signed_score"), item.get("raw_score")]
                   for item in report["sections"]]
    _append_table(sections_sheet, section_headers, section_rows)

    coverage_sheet = workbook.create_sheet("Coverage")
    coverage_headers = ["record_type", "source_ref", "page_number", "section_path", "matched_chunk_ids",
                        "coverage_status", "source_excerpt", "explanation"]
    coverage_rows = []
    for item in report["coverage"]["unmatched_docx_units"]:
        coverage_rows.append(["DOCX_SOURCE_UNIT", item["source_ref"], "", item["section_path"], "",
                              "MISSING_FROM_CHUNKS", item["source_excerpt"],
                              "Supported by PDF" if item["supported_by_pdf"] else "PDF support is uncertain; review upstream conversion."])
    for item in report["coverage"]["pdf_docx_upstream_issues"]:
        coverage_rows.append(["PDF_DOCX_UPSTREAM_ISSUE", item["source_ref"], item["page_number"], "", "",
                              item["classification"], item["source_excerpt"], item["explanation"]])
    if not coverage_rows:
        coverage_rows.append(["SUMMARY", "", "", "", "", "NO_UNMATCHED_SOURCE_UNITS",
                              "", f"{report['coverage']['covered_source_units']} of {report['coverage']['eligible_source_units']} DOCX units covered."])
    _append_table(coverage_sheet, coverage_headers, coverage_rows)

    criteria_sheet = workbook.create_sheet("Detailed Checks")
    criteria_headers = ["criterion_id", "category", "criterion", "scope", "evaluation_method", "PASS",
                        "FAIL", "N/A", "INCONCLUSIVE", "scorable", "pass_percentage", "signed_score"]
    criteria_rows = [[item.get("id"), item.get("category"), item.get("name"), item.get("scope"),
                      item.get("method"), item.get("pass_count"), item.get("fail_count"),
                      item.get("not_applicable_count"), item.get("inconclusive_count"),
                      item.get("scorable_count"), item.get("pass_percentage"), item.get("signed_score")]
                     for item in report["criteria_summary"]]
    _append_table(criteria_sheet, criteria_headers, criteria_rows)

    audit_sheet = workbook.create_sheet("Input Audit")
    audit_rows = [
        ["pdflens_package_sha256", report["inputs"]["pdflens_package_sha256"]],
        ["chunks_sha256", report["inputs"]["chunks_sha256"]],
        ["source_pdf_sha256", report["inputs"]["source_pdf_sha256"]],
        ["source_docx_sha256", report["inputs"]["source_docx_sha256"]],
        ["source_pdf_member", report["inputs"]["source_pdf_member"]],
        ["source_docx_member", report["inputs"]["source_docx_member"]],
        ["PDF page count", report["inputs"]["pdf_page_count"]],
        ["chunk input count", report["inputs"]["chunk_input_count"]],
        ["alignment source", report["source_kind_for_alignment"]],
        ["criteria version", report["criteria_version"]],
        ["LLM settings", report["llm"]],
        ["evidence notes", report["evidence_notes"]],
        ["package members", report["inputs"]["package_members"]],
        ["configuration", report["configuration"]],
    ]
    audit_sheet.append(["Field", "Value"])
    for cell in audit_sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = Font(bold=True, color="FFFFFF")
    for row in audit_rows:
        audit_sheet.append([_excel_safe(value) for value in row])
    audit_sheet.column_dimensions["A"].width = 34
    audit_sheet.column_dimensions["B"].width = 100
    for row in audit_sheet.iter_rows(min_row=2):
        row[0].fill = _SUBHEADER_FILL
        row[0].font = Font(bold=True)
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def report_json_bytes(report: dict[str, Any]) -> bytes:
    return (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
