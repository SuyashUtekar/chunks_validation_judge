from __future__ import annotations

from collections import Counter
from typing import Any

# Five human-readable questions used to review section-wise chunks.
_SCORE_DIMENSIONS = {
    "Meaning preserved": (
        {"C02"},
        "Does the chunk preserve the source's facts, numbers, conditions, exceptions, and meaning?",
    ),
    "Right heading": (
        {"S01"},
        "Does the section path match the source heading and the chunk's subject?",
    ),
    "One subject": (
        {"M01"},
        "Does the chunk stay on one topic or a closely related group of points?",
    ),
    "Natural boundaries": (
        {"B03"},
        "Does the chunk start and end without breaking a sentence or separating a necessary idea?",
    ),
    "Enough context": (
        {"K05"},
        "Can a reader understand the chunk using its heading and included text?",
    ),
}


def _score(findings: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(item["outcome"] for item in findings)
    passed, failed = counts["PASS"], counts["FAIL"]
    total = passed + failed
    score_points = passed - failed
    return {
        "pass_count": passed,
        "fail_count": failed,
        "inconclusive_count": counts["INCONCLUSIVE"],
        "not_applicable_count": counts["N/A"],
        "scorable_count": total,
        "score_points": score_points,
        "max_points": total,
        "pass_percentage": round(100 * passed / total, 2) if total else None,
    }


def build_scorecard(findings: list[dict[str, Any]], *, include_document: bool = True) -> dict[str, Any]:
    """Summarize findings using five reviewer-friendly chunk questions."""
    dimensions = []
    core_findings = []
    for name, (criterion_ids, description) in _SCORE_DIMENSIONS.items():
        selected = [item for item in findings
                    if item.get("criterion_id") in criterion_ids
                    and item.get("method") == "llm"
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

    counts = Counter(item.get("outcome") for item in core_findings)
    conclusive = counts["PASS"] + counts["FAIL"]
    overall = round(100 * counts["PASS"] / conclusive, 2) if conclusive else None
    net_points = sum(item["score_points"] for item in dimensions)
    max_points = sum(item["max_points"] for item in dimensions)
    return {
        "overall_score": overall,
        "net_score_points": net_points,
        "max_score_points": max_points,
        "scored_dimensions": sum(item["scorable_count"] > 0 for item in dimensions),
        "dimension_count": len(dimensions),
        "pass_count": counts["PASS"],
        "fail_count": counts["FAIL"],
        "inconclusive_count": counts["INCONCLUSIVE"],
        "dimensions": dimensions,
    }


def score_criterion_ids() -> set[str]:
    """Return only criteria that contribute to the sectionwise score and gate."""
    return set().union(*(criterion_ids for criterion_ids, _ in _SCORE_DIMENSIONS.values()))


def core_critical_failure(findings: list[dict[str, Any]]) -> bool:
    selected_ids = score_criterion_ids()
    return any(
        item.get("criterion_id") in selected_ids
        and item.get("outcome") == "FAIL"
        and item.get("severity") == "CRITICAL"
        for item in findings
    )
