from __future__ import annotations

from collections import defaultdict
from typing import Any

_SCORE_CRITERIA = {"C02", "S01", "M01", "B03", "K05"}
_ACTIONS = {
    "coverage": ("Check source text coverage", "Use Coverage to compare DOCX passages with chunks. Add missing DOCX text to the appropriate chunk. If it is absent from the DOCX, review PDFLens conversion first."),
    "content": ("Verify content against the source", "Compare the cited source and chunk excerpts. Correct only evidence-backed changes to facts, numbers, conditions, negation, or meaning."),
    "layout": ("Review section assignment and chunk boundaries", "Compare section paths and chunk excerpts with DOCX headings. Keep adjacent related subsections together when coherent; change paths or boundaries only for a demonstrated mismatch or loss of meaning."),
    "context": ("Check the context", "Use the cited source passage and section path to see whether the chunk has enough context. Add context only when the DOCX supports it."),
    "traceability": ("Fix chunk IDs or section paths", "Check the cited chunk IDs and section paths against the supplied DOCX and chunk file."),
}


def _action_key(finding: dict[str, Any]) -> str:
    criterion = str(finding.get("criterion_id") or "")
    if criterion in {"C01", "C03"}:
        return "coverage"
    if criterion in {"C02", "C05"}:
        return "content"
    if criterion == "K05":
        return "context"
    if criterion.startswith(("S", "M", "B")):
        return "layout"
    return "traceability"


def _action(priority: str, title: str, instruction: str, findings: list[dict[str, Any]], source_refs: list[str] | None = None) -> dict[str, Any]:
    chunk_ids = sorted({item.get("chunk_id") for item in findings if item.get("chunk_id")})
    refs = sorted({ref for item in findings for ref in item.get("source_evidence_refs", [])})
    refs.extend(ref for ref in (source_refs or []) if ref and ref not in refs)
    return {
        "priority": priority, "title": title, "what_to_do": instruction,
        "finding_count": len(findings),
        "finding_ids": [item.get("finding_id") for item in findings if item.get("finding_id")][:8],
        "criterion_ids": sorted({item.get("criterion_id") for item in findings if item.get("criterion_id")}),
        "affected_chunk_count": len(chunk_ids), "chunk_ids": chunk_ids[:3],
        "more_chunk_ids_count": max(0, len(chunk_ids) - 3),
        "source_refs": refs[:5], "more_source_refs_count": max(0, len(refs) - 5),
    }


def build_action_plan(gate: str, findings: list[dict[str, Any]], unmatched_docx_units: list[dict[str, Any]],
                      upstream_issues: list[dict[str, Any]], *, llm_info: dict[str, Any] | None = None,
                      overall_score: float | None = None, review_boundary: float | None = None) -> dict[str, Any]:
    """Summarize primary-scorecard issues as a short set of evidence-linked next steps."""
    llm_info = llm_info or {}
    unresolved_llm = [item for item in findings if item.get("criterion_id") in _SCORE_CRITERIA
                      and item.get("outcome") == "INCONCLUSIVE"]
    primary = [item for item in findings if item.get("criterion_id") in _SCORE_CRITERIA
               and item.get("outcome") == "FAIL"]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in primary:
        grouped[_action_key(item)].append(item)
    actions = []
    for key, related in grouped.items():
        title, instruction = _ACTIONS[key]
        failures = [item for item in related if item.get("outcome") == "FAIL"]
        priority = "High" if any(item.get("severity") in {"CRITICAL", "HIGH"} for item in failures) else ("Medium" if failures else "Review")
        refs = [item.get("source_ref") for item in unmatched_docx_units] if key == "coverage" else []
        actions.append(_action(priority, title, instruction, related, refs))
    if upstream_issues:
        refs = [item.get("source_ref") for item in upstream_issues]
        issue = {"criterion_id": "C03", "outcome": "FAIL", "severity": "HIGH", "source_evidence_refs": refs}
        action = _action("High", "Check possible PDF-to-DOCX omissions",
                         "Compare the listed PDF passages with the DOCX. If text is missing from the DOCX, review PDFLens conversion before changing chunk boundaries.", [issue], refs)
        action["pages"] = sorted({item.get("page_number") for item in upstream_issues if item.get("page_number")})
        actions.append(action)
    if unresolved_llm:
        if not llm_info.get("configured"):
            instruction = "Configure the LiteLLM URL, model alias, and key, then rerun. These checks were not evaluated and are not failures."
            title = "Configure LiteLLM to score the criteria"
        elif llm_info.get("failed_chunk_calls"):
            instruction = (f"LiteLLM settings are present, but {llm_info['failed_chunk_calls']} chunk calls failed. "
                           "Check the server log for the HTTP or response-format error, then verify the proxy URL, model alias, key, and JSON mode before rerunning.")
            title = "Fix failed LiteLLM calls"
        elif llm_info.get("chunks_without_source_evidence"):
            instruction = (f"{llm_info['chunks_without_source_evidence']} chunks had no sufficiently aligned DOCX passage. "
                           "Confirm the uploaded ZIP contains the DOCX used to create this chunk JSON, then review the listed source/chunk evidence and rerun.")
            title = "Check DOCX-to-chunk alignment"
        else:
            instruction = ("The model responded, but some criterion results were inconclusive or failed reference validation. "
                           "Open Scored evidence and review the reason and cited passages for each check before changing the chunks.")
            title = "Review inconclusive LLM results"
        actions.append({"priority": "Review", "title": title, "what_to_do": instruction,
                        "finding_count": len(unresolved_llm), "finding_ids": [],
                        "criterion_ids": sorted({item.get("criterion_id") for item in unresolved_llm if item.get("criterion_id")}),
                        "affected_chunk_count": len({item.get("chunk_id") for item in unresolved_llm if item.get("chunk_id")}),
                        "chunk_ids": sorted({item.get("chunk_id") for item in unresolved_llm if item.get("chunk_id")})[:3],
                        "more_chunk_ids_count": max(0, len({item.get("chunk_id") for item in unresolved_llm if item.get("chunk_id")}) - 3),
                        "source_refs": [], "more_source_refs_count": 0})
    if gate == "FAIL" and overall_score is not None and review_boundary is not None:
        summary = f"Needs correction: the criteria score is {overall_score:.1f}%, below the {review_boundary:.0f}% minimum review threshold. See the cited source and chunk comparisons in the review queue."
    elif gate == "FAIL":
        summary = "Needs correction: one or more scored criteria failed. Review the cited evidence and rerun the judge."
    elif gate == "REVIEW_REQUIRED":
        summary = "Needs review: the score is in the review band or some criteria could not be assessed. Unresolved checks are not counted as failures."
    elif gate == "PASS":
        summary = "Meets the current score threshold: all scored LLM criteria have enough evidence and the score is at or above the pass threshold."
    else:
        summary = "No criteria score is available. Check that the DOCX, chunks, and LiteLLM configuration were supplied."
    rank = {"High": 0, "Medium": 1, "Review": 2}
    actions.sort(key=lambda item: (rank[item["priority"]], item["title"]))
    return {"summary": summary, "actions": actions}
