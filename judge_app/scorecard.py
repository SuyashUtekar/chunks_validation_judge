from __future__ import annotations

from collections import Counter
from typing import Any

# Five top-level measures; the individual findings remain available as supporting evidence.
_SCORE_DIMENSIONS = {
    "Content coverage": ({"C01", "C03"}, "Checks whether DOCX source text is represented in the chunks."),
    "Content fidelity": ({"C02", "C05", "C06"}, "Checks meaning, key facts, numbers, conditions, and negation against supplied source evidence."),
    "Section placement": ({"S01", "S02", "S03", "S04", "S05"}, "Checks chunk paths and section coverage against the DOCX heading hierarchy."),
    "Chunk coherence and usefulness": ({"M01", "M02", "M03", "M05", "B01", "B03"}, "Checks that chunk boundaries preserve a readable, useful idea."),
    "Traceability": ({"T01", "T03"}, "Checks that chunk IDs are unique and section paths resolve."),
}


def _score(findings: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(item["outcome"] for item in findings)
    passed, failed = counts["PASS"], counts["FAIL"]
    total = passed + failed
    return {
        "pass_count": passed,
        "fail_count": failed,
        "inconclusive_count": counts["INCONCLUSIVE"],
        "not_applicable_count": counts["N/A"],
        "scorable_count": total,
        "pass_percentage": round(100 * passed / total, 2) if total else None,
    }


def build_scorecard(findings: list[dict[str, Any]], *, include_document: bool = True) -> dict[str, Any]:
    """Summarize findings into a few auditable, non-overlapping quality dimensions."""
    dimensions = []
    core_findings = []
    for name, (criterion_ids, description) in _SCORE_DIMENSIONS.items():
        selected = [item for item in findings
                    if item.get("criterion_id") in criterion_ids
                    and (include_document or item.get("scope") != "document")]
        if not selected:
            continue
        core_findings.extend(selected)
        score = _score(selected)
        if score["scorable_count"]:
            if score["fail_count"] == 0:
                status = "PASS"
            elif score["pass_count"] == 0:
                status = "FAIL"
            else:
                status = "MIXED"
        elif score["inconclusive_count"]:
            status = "INCONCLUSIVE"
        else:
            status = "NOT_APPLICABLE"
        dimensions.append({
            "dimension": name,
            "what_it_checks": description,
            "status": status,
            "criteria": sorted(criterion_ids),
            **score,
        })

    scored = [item["pass_percentage"] for item in dimensions if item["pass_percentage"] is not None]
    overall = round(sum(scored) / len(scored), 2) if scored else None
    counts = Counter(item.get("outcome") for item in core_findings)
    return {
        "overall_score": overall,
        "scored_dimensions": len(scored),
        "dimension_count": len(dimensions),
        "pass_count": counts["PASS"],
        "fail_count": counts["FAIL"],
        "inconclusive_count": counts["INCONCLUSIVE"],
        "dimensions": dimensions,
    }


def core_critical_failure(findings: list[dict[str, Any]]) -> bool:
    selected_ids = set().union(*(criterion_ids for criterion_ids, _ in _SCORE_DIMENSIONS.values()))
    return any(
        item.get("criterion_id") in selected_ids
        and item.get("outcome") == "FAIL"
        and item.get("severity") == "CRITICAL"
        for item in findings
    )
