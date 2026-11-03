from __future__ import annotations

from collections import defaultdict
from typing import Any

_ACTIONS = {
    "coverage": (
        "Check missing source text",
        "Open Coverage. If the text exists in the DOCX but not in any chunk, correct the chunk coverage. If it is absent from the DOCX, address PDF-to-DOCX extraction first.",
    ),
    "content": (
        "Compare chunk text with its source",
        "Compare the cited source and chunk excerpts. Correct only demonstrated changes to facts, numbers, conditions, negation, or meaning.",
    ),
    "section": (
        "Review section paths and heading coverage",
        "Compare the chunk path with the DOCX headings and source excerpt. Keep adjacent related subsections together when they remain coherent; change paths or boundaries only when the evidence shows a meaningful mismatch or missing section.",
    ),
    "boundary": (
        "Review chunk boundaries for meaning",
        "Check the cited chunk edge and nearby source context. Split or combine content only when a sentence, condition, definition, or related idea has been separated in a way that harms understanding or retrieval.",
    ),
    "size": (
        "Review chunk size",
        "Compare the chunk with the configured size limit. Adjust only chunks that exceed the supplied limit or are too small to carry useful context.",
    ),
    "context": (
        "Supply the missing context",
        "Check whether the definition, reference, table/figure caption, or neighboring chunk is available. Add or link the needed context only when the source supports it.",
    ),
    "traceability": (
        "Repair chunk identifiers or traceability",
        "Check the affected chunk IDs, section paths, page references, sequence, and required metadata against the supplied source and configuration.",
    ),
    "evidence": (
        "Resolve checks that lack enough evidence",
        "Compare the cited source and chunk excerpts, provide relevant neighboring context or entity information, and configure the on-prem LLM for contextual checks if it is available. Do not treat an inconclusive result as a proven defect.",
    ),
    "upstream": (
        "Check possible PDF-to-DOCX omissions",
        "Review the listed PDF text against the DOCX. If it is missing from the DOCX, fix or review PDFLens conversion; changing chunk boundaries cannot restore text that never reached the DOCX.",
    ),
}


def _action_key(finding: dict[str, Any]) -> str:
    criterion = str(finding.get("criterion_id") or "")
    if criterion in {"C01", "C03"}:
        return "coverage"
    if criterion in {"C02", "C04", "C05", "C06", "C07"}:
        return "content"
    if criterion.startswith("S"):
        return "section"
    if criterion == "B04":
        return "size"
    if criterion.startswith("B"):
        return "boundary"
    if criterion.startswith(("K", "E")):
        return "context"
    if criterion.startswith("T"):
        return "traceability"
    if criterion.startswith("M"):
        return "boundary"
    return "evidence"


def build_action_plan(
    gate: str,
    findings: list[dict[str, Any]],
    unmatched_docx_units: list[dict[str, Any]],
    upstream_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Turn evaluator results into concise, evidence-linked next steps."""
    if gate == "PASS":
        summary = "The checks meet the current provisional thresholds. Review any unresolved items below before accepting the result."
    elif gate == "FAIL":
        summary = "The quality gate failed. Start with high-priority findings, then re-run the judge after corrections."
    elif gate == "REVIEW_REQUIRED":
        summary = "Human review is needed. Some checks failed or lack enough evidence; an inconclusive result is not proof of a defect."
    else:
        summary = "The judge could not score enough criteria to make a quality decision. Check the inputs and available evidence."

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        if finding.get("outcome") in {"FAIL", "INCONCLUSIVE"}:
            grouped[_action_key(finding)].append(finding)

    actions: list[dict[str, Any]] = []
    for key, related in grouped.items():
        title, instruction = _ACTIONS[key]
        failures = [item for item in related if item.get("outcome") == "FAIL"]
        severities = {str(item.get("severity") or "").upper() for item in failures}
        if "CRITICAL" in severities or "HIGH" in severities:
            priority = "High"
        elif failures:
            priority = "Medium"
        else:
            priority = "Review"
        actions.append({
            "priority": priority,
            "title": title,
            "what_to_do": instruction,
            "finding_ids": [item.get("finding_id") for item in related if item.get("finding_id")],
            "criterion_ids": sorted({item.get("criterion_id") for item in related if item.get("criterion_id")}),
            "chunk_ids": sorted({item.get("chunk_id") for item in related if item.get("chunk_id")}),
            "source_refs": sorted({ref for item in related for ref in item.get("source_evidence_refs", [])}),
        })

    if unmatched_docx_units:
        actions.append({
            "priority": "High" if any(item.get("supported_by_pdf") for item in unmatched_docx_units) else "Review",
            "title": "Check DOCX source text not represented in chunks",
            "what_to_do": "Use Coverage to compare each unmatched DOCX passage with the chunk set. Add it to the appropriate chunk if it belongs there; if the DOCX passage itself is incorrect or incomplete, review PDFLens conversion first.",
            "finding_ids": [],
            "criterion_ids": ["C03"],
            "chunk_ids": [],
            "source_refs": [item.get("source_ref") for item in unmatched_docx_units[:30]],
        })
    if upstream_issues:
        actions.append({
            "priority": "High",
            "title": _ACTIONS["upstream"][0],
            "what_to_do": _ACTIONS["upstream"][1],
            "finding_ids": [],
            "criterion_ids": ["C03"],
            "chunk_ids": [],
            "source_refs": [item.get("source_ref") for item in upstream_issues[:30]],
            "pages": sorted({item.get("page_number") for item in upstream_issues if item.get("page_number")}),
        })

    priority_rank = {"High": 0, "Medium": 1, "Review": 2}
    actions.sort(key=lambda item: (priority_rank[item["priority"]], item["title"]))
    return {"summary": summary, "actions": actions}
