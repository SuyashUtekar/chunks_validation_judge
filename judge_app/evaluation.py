from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
import uuid

from . import __version__
from .action_plan import build_action_plan
from .scorecard import build_scorecard, core_critical_failure
from .evidence import build_source_evidence
from .ingestion import InputBundle
from .llm import LLMUnavailableError, OpenAICompatibleJudge

_APP_ROOT = Path(__file__).resolve().parents[1]
_CRITERIA = json.loads((_APP_ROOT / "judge_app" / "criteria.json").read_text(encoding="utf-8"))
_CRITERIA_BY_ID = {item["id"]: item for item in _CRITERIA["criteria"]}
_OUTCOMES = {"PASS", "FAIL", "N/A", "INCONCLUSIVE"}
_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
_LLM_IDS = {item["id"] for item in _CRITERIA["criteria"] if item["method"] == "llm"}
_STOPWORDS = set("a an and are as at be by for from in into is it of on or that the this to was were with".split())
_TOKEN_RE = re.compile(r"[\w]+(?:[.,:/%-][\w]+)*", re.UNICODE)
_NUMBER_RE = re.compile(r"(?<!\w)(?:\d{1,4}[-/.]){1,2}\d{1,4}|\d[\d,]*(?:\.\d+)?\s*%?", re.UNICODE)
_VISUAL_TAG_RE = re.compile(
    r"(?:<doc_ai\s+type=['\"](?:image|table)['\"]|\[\[(?:IMAGE|TABLE):)", re.IGNORECASE
)


def _normalize(text: str) -> str:
    return " ".join(token.casefold() for token in _TOKEN_RE.findall(str(text or "")))


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(str(text or ""))]


def _key(text: str) -> str:
    return re.sub(r"[^\w]+", " ", str(text or "").casefold()).strip()


def _truncate(text: str, limit: int = 320) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _content_and_path(chunk: dict[str, Any], source_sections: list[dict[str, Any]]) -> None:
    parts = [part.strip() for part in chunk["content"].split("|")]
    path = chunk["section_path"]
    if path:
        consumed = 0
        for title in path:
            if consumed < len(parts) and _key(parts[consumed]) == _key(title):
                consumed += 1
            else:
                break
        if consumed:
            chunk["body_content"] = " | ".join(parts[consumed:]).strip()
            return
    else:
        inferred = []
        matched_paths = []
        for part in parts[:-1]:
            candidate = next((section for section in source_sections
                              if _key(section["title"]) == _key(part)), None)
            if candidate is None:
                break
            inferred.append(part)
            matched_paths = candidate["section_path"]
        if inferred:
            chunk["section_path"] = list(matched_paths or inferred)
            chunk["body_content"] = " | ".join(parts[len(inferred):]).strip()
            return
    chunk["body_content"] = chunk["content"].strip()


def _alignment(chunk_text: str, unit_text: str) -> tuple[float, float, float]:
    chunk_tokens = [token for token in _tokens(chunk_text) if token not in _STOPWORDS]
    unit_tokens = [token for token in _tokens(unit_text) if token not in _STOPWORDS]
    if not chunk_tokens or not unit_tokens:
        return 0.0, 0.0, 0.0
    chunk_counts, unit_counts = Counter(chunk_tokens), Counter(unit_tokens)
    shared = sum((chunk_counts & unit_counts).values())
    precision = shared / max(1, sum(chunk_counts.values()))
    recall = shared / max(1, sum(unit_counts.values()))
    score = 0.7 * recall + 0.3 * precision
    chunk_norm, unit_norm = _normalize(chunk_text), _normalize(unit_text)
    if chunk_norm and (chunk_norm in unit_norm or unit_norm in chunk_norm):
        score = max(score, 0.98)
        recall = max(recall, 0.98)
    return score, precision, recall


def _match_chunk(chunk: dict[str, Any], units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content = chunk["body_content"] or chunk["content"]
    candidates = []
    for unit in units:
        score, precision, recall = _alignment(content, unit["text"])
        if score > 0:
            candidates.append({"unit": unit, "score": score, "precision": precision, "recall": recall})
    candidates.sort(key=lambda item: (item["score"], item["recall"], item["precision"]), reverse=True)
    return candidates[:5]


def _covered_unit(unit: dict[str, Any], chunks: list[dict[str, Any]]) -> tuple[bool, list[str], float]:
    unit_tokens = [token for token in _tokens(unit["text"]) if token not in _STOPWORDS]
    unit_vocabulary = set(unit_tokens)
    covered_tokens: set[str] = set()
    matches: list[tuple[str, float]] = []
    for chunk in chunks:
        tokens = {token for token in _tokens(chunk["body_content"] or chunk["content"])
                  if token not in _STOPWORDS}
        shared = tokens & unit_vocabulary
        if shared:
            covered_tokens.update(shared)
            precision = len(shared) / max(1, len(tokens))
            matches.append((chunk["chunk_id"], precision))
    recall = len(covered_tokens) / max(1, len(unit_vocabulary))
    precision = max((item[1] for item in matches), default=0.0)
    return recall >= 0.78 and precision >= 0.12, [item[0] for item in matches], recall


def _path_resolves(path: list[str], headings: list[dict[str, Any]]) -> bool:
    if not path:
        return False
    candidates = {tuple(_key(part) for part in item["section_path"])
                  for item in headings if item.get("section_path")}
    candidate = tuple(_key(part) for part in path)
    return candidate in candidates or any(candidate == item[:len(candidate)] for item in candidates)


def _finding(criterion_id: str, outcome: str, explanation: str, *, chunk: dict[str, Any] | None = None,
             source_refs: list[str] | None = None, chunk_refs: list[str] | None = None,
             source_excerpt: str = "", chunk_excerpt: str = "", severity: str | None = None,
             confidence: float | None = None, human_review: bool = False,
             scope: str | None = None) -> dict[str, Any]:
    criterion = _CRITERIA_BY_ID[criterion_id]
    return {
        "finding_id": "",
        "chunk_id": chunk["chunk_id"] if chunk else "",
        "sequence_number": chunk.get("sequence_number") if chunk else None,
        "scope": scope or criterion["scope"],
        "criterion_id": criterion_id,
        "category": criterion["category"],
        "criterion": criterion["name"],
        "outcome": outcome,
        "severity": severity if outcome == "FAIL" else None,
        "explanation": explanation,
        "source_evidence_refs": source_refs or [],
        "chunk_evidence_refs": chunk_refs or ([f"chunk:{chunk['chunk_id']}:content"] if chunk else []),
        "source_excerpt": _truncate(source_excerpt),
        "chunk_excerpt": _truncate(chunk_excerpt or (chunk["content"] if chunk else "")),
        "confidence": confidence,
        "human_review_required": human_review,
        "method": criterion["method"],
    }


def _add_llm_findings(chunks: list[dict[str, Any]], matches: dict[str, list[dict[str, Any]]],
                      findings: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    try:
        judge = OpenAICompatibleJudge()
    except LLMUnavailableError as exc:
        for chunk in chunks:
            for criterion_id in sorted(_LLM_IDS):
                if criterion_id == "K03" and not _VISUAL_TAG_RE.search(chunk["content"]):
                    findings.append(_finding(criterion_id, "N/A", "Chunk contains no table/image tag.", chunk=chunk))
                elif criterion_id in {"E03", "E04"} and not chunk.get("entities") and not config.get("entity_map"):
                    findings.append(_finding(criterion_id, "N/A", "No entity map was supplied.", chunk=chunk))
                else:
                    findings.append(_finding(criterion_id, "INCONCLUSIVE", str(exc), chunk=chunk,
                                             human_review=True))
        return {"configured": False, "provider": None, "model": None,
                "note": "Contextual criteria were not scored because an on-prem endpoint is not configured."}

    llm_errors = 0
    for chunk in chunks:
        candidates = matches.get(chunk["chunk_id"], [])
        evidence_units = [candidate["unit"] for candidate in candidates[:3] if candidate["score"] >= 0.18]
        refs = [unit["unit_id"] for unit in evidence_units]
        chunk_ref = f"chunk:{chunk['chunk_id']}:content"
        relevant_ids = sorted(_LLM_IDS)
        if not chunk.get("entities") and not config.get("entity_map"):
            relevant_ids = [criterion_id for criterion_id in relevant_ids if criterion_id not in {"E03", "E04"}]
        if "K03" in relevant_ids and not _VISUAL_TAG_RE.search(chunk["content"]):
            relevant_ids.remove("K03")
            findings.append(_finding("K03", "N/A", "Chunk contains no table/image tag.", chunk=chunk))
        if not evidence_units:
            for criterion_id in relevant_ids:
                findings.append(_finding(criterion_id, "INCONCLUSIVE",
                                         "No reliable source passage aligned to this chunk.", chunk=chunk,
                                         chunk_refs=[chunk_ref], human_review=True))
            continue
        payload = {
            "criteria": [{"criterion_id": cid, "name": _CRITERIA_BY_ID[cid]["name"]}
                         for cid in relevant_ids],
            "chunk": {"chunk_id": chunk["chunk_id"],
                      "content": _truncate(chunk["body_content"] or chunk["content"], 8000),
                      "section_path": chunk["section_path"]},
            "source_evidence": [
                {"evidence_ref": unit["unit_id"], "section_path": unit["section_path"],
                 "page_number": unit["page_number"], "text": _truncate(unit["text"], 1800)}
                for unit in evidence_units
            ],
            "deterministic_alignment": [
                {"evidence_ref": candidate["unit"]["unit_id"],
                 "text_match_score": round(candidate["score"], 3)}
                for candidate in candidates[:3]
            ],
            "chunk_evidence_ref": chunk_ref,
            "chunking_configuration": {key: value for key, value in config.items()
                                       if key != "entity_map"},
        }
        try:
            response = judge.judge(payload)
            returned = {}
            for item in response["criteria"]:
                cid = item.get("criterion_id")
                if cid not in relevant_ids or cid in returned:
                    continue
                outcome = item.get("outcome")
                if outcome not in _OUTCOMES:
                    continue
                source_refs = item.get("source_evidence_refs", [])
                chunk_refs = item.get("chunk_evidence_refs", [])
                if not isinstance(source_refs, list) or not isinstance(chunk_refs, list):
                    continue
                if any(ref not in refs for ref in source_refs) or any(ref != chunk_ref for ref in chunk_refs):
                    continue
                if outcome == "FAIL" and (not source_refs or not chunk_refs):
                    outcome = "INCONCLUSIVE"
                severity = item.get("severity")
                if severity not in _SEVERITIES:
                    severity = None
                try:
                    confidence = max(0.0, min(1.0, float(item.get("confidence"))))
                except (TypeError, ValueError):
                    confidence = None
                findings.append(_finding(
                    cid, outcome, _truncate(str(item.get("explanation") or "No explanation supplied."), 1000),
                    chunk=chunk, source_refs=source_refs, chunk_refs=chunk_refs,
                    source_excerpt=evidence_units[0]["text"], chunk_excerpt=chunk["content"],
                    severity=severity, confidence=confidence,
                    human_review=bool(item.get("human_review_required", outcome in {"FAIL", "INCONCLUSIVE"})),
                ))
                returned[cid] = True
            for cid in relevant_ids:
                if cid not in returned:
                    findings.append(_finding(cid, "INCONCLUSIVE", "Judge response omitted or invalidated this criterion.",
                                             chunk=chunk, source_refs=refs[:1], chunk_refs=[chunk_ref],
                                             human_review=True))
        except LLMUnavailableError as exc:
            llm_errors += 1
            for cid in relevant_ids:
                findings.append(_finding(cid, "INCONCLUSIVE", str(exc), chunk=chunk,
                                         source_refs=refs[:1], chunk_refs=[chunk_ref], human_review=True))
    return {"configured": True, "provider": "openai_compatible_on_prem",
            "model": judge.model, "failed_chunk_calls": llm_errors,
            "prompt_version": "chunk_judge_v4"}


def _criterion_score(findings: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(item["outcome"] for item in findings)
    passed, failed = counts["PASS"], counts["FAIL"]
    denominator = passed + failed
    return {"pass_count": passed, "fail_count": failed,
            "not_applicable_count": counts["N/A"], "inconclusive_count": counts["INCONCLUSIVE"],
            "scorable_count": denominator, "raw_score": passed - failed,
            "pass_percentage": round(100 * passed / denominator, 2) if denominator else None,
            "signed_score": round(100 * (passed - failed) / denominator, 2) if denominator else None}


def _chunk_gate(findings: list[dict[str, Any]], thresholds: dict[str, Any]) -> str:
    scorecard = build_scorecard(findings, include_document=False)
    if core_critical_failure(findings):
        return "FAIL"
    if not scorecard["scored_dimensions"]:
        return "REVIEW_REQUIRED" if scorecard["inconclusive_count"] else "NOT_EVALUATED"
    score = scorecard["overall_score"]
    if score < float(thresholds["review_required_below"]):
        return "FAIL"
    if score < float(thresholds["pass_candidate_at"]) or scorecard["inconclusive_count"]:
        return "REVIEW_REQUIRED"
    return "PASS"


def evaluate(bundle: InputBundle, chunks: list[dict[str, Any]], config: dict[str, Any],
             chunks_sha256: str) -> dict[str, Any]:
    evidence = build_source_evidence(bundle)
    source_units = evidence["docx_units"] or evidence["pdf_units"]
    source_kind = "docx" if evidence["docx_units"] else "pdf"
    sections = evidence["sections"]
    for chunk in chunks:
        _content_and_path(chunk, sections)
    matches = {chunk["chunk_id"]: _match_chunk(chunk, source_units) for chunk in chunks}

    # Use stable, non-empty identifiers in reports while preserving duplicate-ID findings.
    identifier_counts = Counter(chunk["chunk_id"] for chunk in chunks if chunk["chunk_id"])
    for chunk in chunks:
        if not chunk["chunk_id"]:
            chunk["chunk_id"] = f"missing-id-row-{chunk['row_number']:06d}"
            chunk["id_missing"] = True
        else:
            chunk["id_missing"] = False
        chunk["id_duplicate"] = identifier_counts.get(chunk["chunk_id"], 0) > 1
    # Re-key because blank IDs received stable placeholders.
    matches = {chunk["chunk_id"]: _match_chunk(chunk, source_units) for chunk in chunks}

    findings: list[dict[str, Any]] = []
    duplicate_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        normalized_content = _normalize(chunk["body_content"] or chunk["content"])
        if normalized_content:
            duplicate_groups[normalized_content].append(chunk)

    # Per-chunk deterministic checks.
    for chunk in chunks:
        chunk_matches = matches.get(chunk["chunk_id"], [])
        best = chunk_matches[0] if chunk_matches else None
        source_refs = [item["unit"]["unit_id"] for item in chunk_matches[:3]]
        source_excerpt = best["unit"]["text"] if best else ""
        alignment_confidence = float(best["score"]) if best else 0.0
        chunk["alignment_confidence"] = round(alignment_confidence, 3)
        chunk["aligned_source_refs"] = source_refs
        chunk["aligned_section_path"] = best["unit"].get("section_path", []) if best else []
        chunk["aligned_pages"] = sorted({item["unit"]["page_number"] for item in chunk_matches[:3]
                                           if item["unit"].get("page_number")})
        chunk_ref = f"chunk:{chunk['chunk_id']}:content"

        if chunk["id_missing"]:
            findings.append(_finding("T01", "FAIL", "Chunk ID is missing.", chunk=chunk,
                                     chunk_refs=[chunk_ref], severity="HIGH", confidence=1.0))
        elif chunk["id_duplicate"]:
            findings.append(_finding("T01", "FAIL", "Chunk ID is duplicated within this evaluation.",
                                     chunk=chunk, chunk_refs=[chunk_ref], severity="HIGH", confidence=1.0))
        else:
            findings.append(_finding("T01", "PASS", "Chunk ID is present and unique.",
                                     chunk=chunk, chunk_refs=[chunk_ref], confidence=1.0))

        if not (chunk["body_content"] or chunk["content"]).strip():
            findings.append(_finding("C04", "FAIL", "Chunk is empty or whitespace-only.", chunk=chunk,
                                     chunk_refs=[chunk_ref], severity="HIGH", confidence=1.0))
        else:
            check_text = re.sub(r"<doc_ai\b[^>]*?/?>|\[\[(?:IMAGE|TABLE):[^\]]+\]\]", " ",
                                chunk["body_content"] or chunk["content"], flags=re.IGNORECASE)
            body_tokens = [token for token in _tokens(check_text) if token not in _STOPWORDS]
            aligned_text = " ".join(item["unit"]["text"] for item in chunk_matches[:5])
            source_tokens = {token for token in _tokens(aligned_text)}
            unsupported = [token for token in body_tokens if token not in source_tokens]
            ratio = len(unsupported) / max(1, len(body_tokens))
            chunk["unsupported_token_ratio"] = round(ratio, 3)
            if not source_units or not best or alignment_confidence < 0.18:
                outcome, explanation, severity = "INCONCLUSIVE", "No reliable source text alignment is available for support checks.", None
            elif ratio <= 0.10:
                outcome, explanation, severity = "PASS", "At least 90% of informative chunk tokens occur in the consumed source text.", None
            elif ratio > 0.35:
                outcome, explanation, severity = "FAIL", f"{ratio:.1%} of informative chunk tokens were not found in source text.", "MEDIUM"
            else:
                outcome, explanation, severity = "INCONCLUSIVE", f"Unsupported-token ratio is {ratio:.1%}; manual or semantic review is needed.", None
            findings.append(_finding("C04", outcome, explanation, chunk=chunk,
                                     source_refs=source_refs[:1], chunk_refs=[chunk_ref],
                                     source_excerpt=source_excerpt, severity=severity,
                                     confidence=round(1 - ratio, 3), human_review=outcome == "INCONCLUSIVE"))

        group = duplicate_groups.get(_normalize(chunk["body_content"] or chunk["content"]), [])
        is_dup = len(group) > 1 and group[0] is not chunk
        findings.append(_finding("C07", "FAIL" if is_dup else "PASS",
                                 "Exact normalized content duplicates an earlier chunk." if is_dup
                                 else "No exact normalized duplicate was found.",
                                 chunk=chunk, chunk_refs=[chunk_ref], severity="MEDIUM" if is_dup else None,
                                 confidence=1.0))

        numbers = _NUMBER_RE.findall(chunk["body_content"] or chunk["content"])
        if not best or alignment_confidence < 0.35:
            findings.append(_finding("C05", "INCONCLUSIVE", "No sufficiently strong source alignment for numeric verification.",
                                     chunk=chunk, source_refs=source_refs[:1], chunk_refs=[chunk_ref],
                                     human_review=True))
        else:
            candidate_source = " ".join(item["unit"]["text"] for item in chunk_matches[:3])
            source_numbers = Counter(re.sub(r"\s+", "", item).casefold()
                                     for item in _NUMBER_RE.findall(candidate_source))
            chunk_numbers = Counter(re.sub(r"\s+", "", item).casefold() for item in numbers)
            missing_numbers = list((source_numbers - chunk_numbers).elements())
            changed_numbers = list((chunk_numbers - source_numbers).elements())
            if missing_numbers or changed_numbers:
                if alignment_confidence >= 0.78:
                    outcome = "FAIL"
                    severity = "HIGH" if missing_numbers else "MEDIUM"
                else:
                    outcome, severity = "INCONCLUSIVE", None
                explanation = (f"Numeric token mismatch in candidate evidence; source-only: {missing_numbers[:8]}, "
                               f"chunk-only: {changed_numbers[:8]}. Strong alignment is required to fail this check.")
            else:
                outcome, severity = "PASS", None
                explanation = "Numeric tokens match across the selected aligned source passages."
            findings.append(_finding("C05", outcome, explanation, chunk=chunk,
                                     source_refs=source_refs[:3], chunk_refs=[chunk_ref],
                                     source_excerpt=candidate_source, severity=severity,
                                     confidence=alignment_confidence))

        if config.get("max_words") is not None:
            actual = len(chunk["content"].split())
            limit = int(config["max_words"])
            outcome = "PASS" if actual <= limit else "FAIL"
            findings.append(_finding("B04", outcome, f"Chunk has {actual} words; configured limit is {limit}.",
                                     chunk=chunk, source_refs=source_refs[:1], chunk_refs=[chunk_ref],
                                     severity="HIGH" if outcome == "FAIL" else None,
                                     confidence=1.0))
        elif config.get("max_characters") is not None:
            actual = len(chunk["content"])
            limit = int(config["max_characters"])
            outcome = "PASS" if actual <= limit else "FAIL"
            findings.append(_finding("B04", outcome,
                                     f"Chunk has {actual} characters; configured limit is {limit}.",
                                     chunk=chunk, source_refs=source_refs[:1], chunk_refs=[chunk_ref],
                                     severity="HIGH" if outcome == "FAIL" else None, confidence=1.0))
        else:
            findings.append(_finding("B04", "INCONCLUSIVE",
                                     "No max_words or max_characters limit was supplied.", chunk=chunk,
                                     chunk_refs=[chunk_ref], human_review=True))

        if chunk["page_numbers"]:
            invalid = [page for page in chunk["page_numbers"] if page < 1 or page > evidence["page_count"]]
            outcome = "FAIL" if invalid else "PASS"
            findings.append(_finding("T02", outcome,
                                     f"Invalid source page references: {invalid}." if invalid
                                     else "All supplied source page references are within the PDF page range.",
                                     chunk=chunk, source_refs=source_refs[:1], chunk_refs=[chunk_ref],
                                     severity="HIGH" if invalid else None, confidence=1.0))
        else:
            findings.append(_finding("T02", "INCONCLUSIVE", "Chunk input contains no source page references.",
                                     chunk=chunk, chunk_refs=[chunk_ref], human_review=True))

        if chunk["section_path"]:
            resolves = _path_resolves(chunk["section_path"], sections)
            findings.append(_finding("T03", "PASS" if resolves else "FAIL",
                                     "Section path resolves to the DOCX heading hierarchy." if resolves
                                     else "Section path does not resolve to a heading in the source DOCX.",
                                     chunk=chunk, source_refs=source_refs[:1], chunk_refs=[chunk_ref],
                                     source_excerpt=" > ".join(chunk["section_path"]),
                                     severity="MEDIUM" if not resolves else None,
                                     confidence=0.95 if resolves else 0.85))
            if best and best["unit"].get("section_path"):
                expected = [_key(part) for part in best["unit"]["section_path"]]
                actual = [_key(part) for part in chunk["section_path"]]
                correct = actual == expected[:len(actual)] or expected == actual[:len(expected)]
                findings.append(_finding("S02", "PASS" if correct else "FAIL",
                                         "Chunk hierarchy agrees with its aligned source section." if correct
                                         else "Chunk section path conflicts with the aligned source section.",
                                         chunk=chunk, source_refs=[best["unit"]["unit_id"]],
                                         chunk_refs=[chunk_ref], source_excerpt=" > ".join(best["unit"]["section_path"]),
                                         severity="HIGH" if not correct else None,
                                         confidence=alignment_confidence))
            else:
                findings.append(_finding("S02", "INCONCLUSIVE",
                                         "A section path exists, but aligned source hierarchy is unavailable.",
                                         chunk=chunk, source_refs=source_refs[:1], chunk_refs=[chunk_ref],
                                         human_review=True))
        else:
            findings.append(_finding("T03", "INCONCLUSIVE", "Chunk has no resolvable section path.",
                                     chunk=chunk, chunk_refs=[chunk_ref], human_review=True))
            findings.append(_finding("S02", "INCONCLUSIVE", "No chunk section hierarchy was supplied or inferred.",
                                     chunk=chunk, chunk_refs=[chunk_ref], human_review=True))

        seq_list = [item["sequence_number"] for item in chunks]
        sequence_available = any(value is not None for value in seq_list)
        has_offsets = chunk["start_offset"] is not None or chunk["end_offset"] is not None
        if sequence_available:
            previous = next((other["sequence_number"] for other in chunks[:chunk["row_number"] - 1]
                             if other["sequence_number"] is not None), None)
            valid = chunk["sequence_number"] is not None and (previous is None or chunk["sequence_number"] > previous)
            findings.append(_finding("T04", "PASS" if valid else "FAIL",
                                     "Supplied sequence is strictly increasing in file order." if valid
                                     else "Sequence number is missing, duplicated, or out of order.",
                                     chunk=chunk, chunk_refs=[chunk_ref],
                                     severity="MEDIUM" if not valid else None, confidence=1.0))
        elif has_offsets:
            try:
                valid = (int(chunk["start_offset"]) >= 0
                         and int(chunk["end_offset"]) > int(chunk["start_offset"]))
            except (TypeError, ValueError):
                valid = False
            findings.append(_finding("T04", "PASS" if valid else "FAIL",
                                     "Supplied source offsets are valid." if valid
                                     else "Supplied source offsets are invalid or reversed.",
                                     chunk=chunk, chunk_refs=[chunk_ref],
                                     severity="MEDIUM" if not valid else None, confidence=1.0))
        else:
            findings.append(_finding("T04", "N/A", "No sequence numbers or source offsets were supplied.", chunk=chunk))

        required_metadata = config.get("required_metadata_fields", [])
        if required_metadata:
            missing = []
            values = {**chunk["raw"], "metadata": chunk["metadata"]}
            for dotted in required_metadata:
                current: Any = values
                for part in str(dotted).split("."):
                    current = current.get(part) if isinstance(current, dict) else None
                if current in (None, "", [], {}):
                    missing.append(str(dotted))
            outcome = "FAIL" if missing else "PASS"
            findings.append(_finding("T05", outcome,
                                     f"Missing required metadata fields: {missing}." if missing
                                     else "All configured required metadata fields are present.",
                                     chunk=chunk, chunk_refs=[chunk_ref],
                                     severity="LOW" if missing else None, confidence=1.0))
        else:
            findings.append(_finding("T05", "N/A", "No required metadata fields were configured.", chunk=chunk))

        entity_map = config.get("entity_map")
        entities = chunk["entities"] or entity_map or []
        if isinstance(entities, dict):
            entities = entities.get("entities", [])
        if isinstance(entities, dict):
            entities = [{"canonical_name": name, "aliases": aliases}
                        for name, aliases in entities.items()]
        if not isinstance(entities, list):
            entities = []
        if entities:
            candidate_text = " ".join(item["unit"]["text"] for item in chunk_matches[:5])
            chunk_specific = bool(chunk["entities"])
            relevant_entities = []
            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                names_value = entity.get("mentions", [])
                aliases_value = entity.get("aliases", [])
                names_value = [names_value] if isinstance(names_value, str) else names_value
                aliases_value = [aliases_value] if isinstance(aliases_value, str) else aliases_value
                names = [entity.get("canonical_name"), *names_value, *aliases_value]
                if chunk_specific or any(str(name).casefold() in candidate_text.casefold()
                                         for name in names if name):
                    relevant_entities.append((entity, names))
            if not relevant_entities:
                findings.append(_finding("E01", "N/A", "No provided entity is present in the aligned source context.", chunk=chunk))
                findings.append(_finding("E02", "N/A", "No source-linked entity needs normalization review.", chunk=chunk))
            else:
                absent = [str(entity.get("canonical_name") or "unknown")
                          for entity, names in relevant_entities
                          if not any(str(name).casefold() in chunk["content"].casefold()
                                     for name in names if name)]
                outcome = "FAIL" if absent else "PASS"
                findings.append(_finding("E01", outcome,
                                         f"Entities not found in the chunk: {absent}." if absent
                                         else "Source-linked entities are mentioned in the chunk.",
                                         chunk=chunk, source_refs=source_refs[:1], chunk_refs=[chunk_ref],
                                         severity="MEDIUM" if absent else None,
                                         confidence=alignment_confidence or 0.8))
                findings.append(_finding("E02", "PASS",
                                         "Entity names were checked against the supplied canonical names and aliases.",
                                         chunk=chunk, chunk_refs=[chunk_ref], confidence=1.0))
        else:
            findings.append(_finding("E01", "N/A", "No entity map or chunk entity metadata was supplied.", chunk=chunk))
            findings.append(_finding("E02", "N/A", "Entity normalization is unavailable without an entity map.", chunk=chunk))

    # Document-level source coverage uses DOCX content as the chunking input.
    eligible_units = [unit for unit in source_units if unit["kind"] in {"paragraph", "table_row"}
                      and len(_tokens(unit["text"])) >= 3]
    unit_matches = {unit["unit_id"]: _covered_unit(unit, chunks) for unit in eligible_units}
    covered_count = sum(1 for value in unit_matches.values() if value[0])
    coverage_pct = 100 * covered_count / len(eligible_units) if eligible_units else None
    missing_units = [unit for unit in eligible_units if not unit_matches[unit["unit_id"]][0]]
    pdf_unit_misses = []
    if evidence["docx_units"]:
        for unit in evidence["pdf_units"]:
            if len(_tokens(unit["text"])) < 3:
                continue
            best_docx = max((_alignment(unit["text"], doc_unit["text"])[0]
                             for doc_unit in evidence["docx_units"]), default=0.0)
            if best_docx < 0.24:
                pdf_unit_misses.append(unit)
    if not eligible_units:
        findings.append(_finding("C01", "INCONCLUSIVE", "No eligible source paragraphs were extracted.",
                                 source_refs=[], chunk_refs=["chunkset:all"], human_review=True, scope="document"))
        findings.append(_finding("C03", "INCONCLUSIVE", "Source coverage could not be measured.",
                                 source_refs=[], chunk_refs=["chunkset:all"], human_review=True, scope="document"))
    elif not evidence["docx_available"]:
        findings.append(_finding("C01", "INCONCLUSIVE", "Converted DOCX was not present; PDF-only source alignment is preliminary.",
                                 source_refs=[], chunk_refs=["chunkset:all"], human_review=True, scope="document"))
        findings.append(_finding("C03", "INCONCLUSIVE", "Missing information cannot be attributed without the consumed DOCX.",
                                 source_refs=[], chunk_refs=["chunkset:all"], human_review=True, scope="document"))
    else:
        source_refs = [unit["unit_id"] for unit in eligible_units[:20]]
        if coverage_pct >= float(config.get("source_unit_coverage_pass_threshold", 85)):
            c01 = "PASS"
        elif coverage_pct < 60:
            c01 = "FAIL"
        else:
            c01 = "INCONCLUSIVE"
        findings.append(_finding("C01", c01,
                                 f"{covered_count}/{len(eligible_units)} eligible DOCX source units are covered by at least one chunk ({coverage_pct:.2f}%).",
                                 source_refs=source_refs, chunk_refs=["chunkset:all"],
                                 severity="HIGH" if c01 == "FAIL" else None,
                                 confidence=round(coverage_pct / 100, 3),
                                 human_review=c01 == "INCONCLUSIVE", scope="document"))
        if missing_units:
            backed_by_pdf = 0
            for unit in missing_units:
                match = max((_alignment(unit["text"], pdf_unit["text"])[0]
                             for pdf_unit in evidence["pdf_units"]), default=0.0)
                if match >= 0.24:
                    backed_by_pdf += 1
            if backed_by_pdf == len(missing_units):
                c03, explanation = "FAIL", f"{len(missing_units)} DOCX source units with PDF support are absent from all chunks."
                severity = "HIGH"
            else:
                c03 = "INCONCLUSIVE"
                explanation = (f"{len(missing_units)} DOCX units are unmatched; only {backed_by_pdf} have clear PDF support, "
                               "so upstream conversion discrepancy may be involved.")
                severity = None
        else:
            c03, explanation, severity = "PASS", "All eligible DOCX source units are represented in one or more chunks.", None
        findings.append(_finding("C03", c03, explanation,
                                 source_refs=[unit["unit_id"] for unit in missing_units[:20]] or source_refs[:5],
                                 chunk_refs=["chunkset:all"], severity=severity,
                                 confidence=0.9 if c03 != "INCONCLUSIVE" else 0.5,
                                 human_review=c03 == "INCONCLUSIVE", scope="document"))

    # Highlight PDF material absent from the DOCX separately as an upstream issue.
    upstream_issues = [{"source_ref": unit["unit_id"], "page_number": unit["page_number"],
                        "source_excerpt": _truncate(unit["text"]),
                        "classification": "UPSTREAM_SOURCE_ISSUE",
                        "explanation": "PDF text block has no sufficiently close match in the PDFLens DOCX."}
                       for unit in pdf_unit_misses]

    source_section_paths = {tuple(item["section_path"]) for item in sections if item.get("section_path")}
    covered_section_paths = set()
    for path in source_section_paths:
        if any(
            chunk["section_path"]
            and (
                tuple(chunk["section_path"][:len(path)]) == path
                or tuple(path[:len(chunk["section_path"])]) == tuple(chunk["section_path"])
            )
            for chunk in chunks
        ):
            covered_section_paths.add(path)
    if not source_section_paths:
        s05 = "INCONCLUSIVE"
        s05_text = "No Word heading hierarchy was available to evaluate section coverage."
    elif len(covered_section_paths) == len(source_section_paths):
        s05 = "PASS"
        s05_text = f"All {len(source_section_paths)} source section paths are represented."
    else:
        s05 = "FAIL"
        s05_text = (f"{len(source_section_paths) - len(covered_section_paths)} of "
                    f"{len(source_section_paths)} source section paths have no associated chunk.")
    findings.append(_finding("S05", s05, s05_text,
                             source_refs=[item["title"] for item in sections[:30]],
                             chunk_refs=["chunkset:all"], severity="MEDIUM" if s05 == "FAIL" else None,
                             confidence=0.85 if s05 != "INCONCLUSIVE" else None,
                             human_review=s05 == "INCONCLUSIVE", scope="document"))

    llm_info = _add_llm_findings(chunks, matches, findings, config)
    for index, finding in enumerate(findings, start=1):
        finding["finding_id"] = f"finding-{index:07d}"

    chunk_reports = []
    for chunk in chunks:
        chunk_findings = [item for item in findings if item["chunk_id"] == chunk["chunk_id"]]
        score = _criterion_score(chunk_findings)
        chunk_scorecard = build_scorecard(chunk_findings, include_document=False)
        chunk_reports.append({
            "chunk_id": chunk["chunk_id"], "sequence_number": chunk["sequence_number"] or chunk["row_number"],
            "section_id": chunk["section_id"], "section_path": chunk["section_path"],
            "page_numbers": chunk["page_numbers"], "word_count": len(chunk["content"].split()),
            "character_count": len(chunk["content"]), "alignment_confidence": chunk["alignment_confidence"],
            "aligned_source_refs": chunk["aligned_source_refs"], "aligned_pages": chunk["aligned_pages"],
            "unsupported_token_ratio": chunk.get("unsupported_token_ratio"), **score,
            "overall_score": chunk_scorecard["overall_score"],
            "scored_dimensions": chunk_scorecard["scored_dimensions"],
            "quality_gate": _chunk_gate(chunk_findings, {
                "pass_candidate_at": config.get("pass_candidate_at", 85),
                "review_required_below": config.get("review_required_below", 65),
            }),
            "chunk_excerpt": _truncate(chunk["content"]),
        })

    section_reports = []
    for path in sorted(source_section_paths, key=lambda item: (len(item), item)):
        section_chunks = [item for item in chunk_reports
                          if item["section_path"] and (
                              tuple(item["section_path"][:len(path)]) == path
                              or tuple(path[:len(item["section_path"])]) == tuple(item["section_path"])
                          )]
        section_findings = [finding for finding in findings
                            if finding["chunk_id"] in {item["chunk_id"] for item in section_chunks}]
        section_score = _criterion_score(section_findings)
        section_reports.append({
            "section_id": "/".join(path), "section_title": path[-1], "section_path": list(path),
            "chunk_count": len(section_chunks), "covered_source_units": sum(
                1 for unit in eligible_units if tuple(unit["section_path"][:len(path)]) == path
                and unit_matches.get(unit["unit_id"], (False, [], 0))[0]),
            "source_unit_count": sum(1 for unit in eligible_units
                                      if tuple(unit["section_path"][:len(path)]) == path),
            "failed_chunks": sum(item["fail_count"] > 0 for item in section_chunks), **section_score,
        })

    total_score = _criterion_score(findings)
    scorecard = build_scorecard(findings)
    pass_pct = scorecard["overall_score"]
    thresholds = {"pass_candidate_at": float(config.get("pass_candidate_at", 85)),
                  "review_required_below": float(config.get("review_required_below", 65))}
    chunk_fail_count = sum(item["fail_count"] > 0 for item in chunk_reports)
    critical = sum(item["severity"] == "CRITICAL" for item in findings
                   if item["outcome"] == "FAIL" and item["criterion_id"] in {
                       "C01", "C03", "C02", "C05", "C06", "S01", "S02", "S03", "S04", "S05",
                       "M01", "M02", "M03", "M05", "B01", "B03", "T01", "T03"})
    high = sum(item["severity"] == "HIGH" for item in findings if item["outcome"] == "FAIL")
    if not scorecard["scored_dimensions"]:
        gate = "REVIEW_REQUIRED" if scorecard["inconclusive_count"] else "NOT_EVALUATED"
    elif critical or (pass_pct is not None and pass_pct < thresholds["review_required_below"]):
        gate = "FAIL"
    elif pass_pct is None or pass_pct < thresholds["pass_candidate_at"] or scorecard["inconclusive_count"]:
        gate = "REVIEW_REQUIRED"
    else:
        gate = "PASS"

    input_metadata = bundle.metadata or {}
    processing = input_metadata.get("processing", {}) if isinstance(input_metadata, dict) else {}
    doc_info = input_metadata.get("document", {}) if isinstance(input_metadata, dict) else {}
    evaluation_id = f"chunk-eval-{uuid.uuid4()}"
    summary = {
        "total_chunks": len(chunks), "chunks_with_failures": chunk_fail_count,
        "chunks_with_review_required": sum(item["quality_gate"] == "REVIEW_REQUIRED" for item in chunk_reports),
        "chunks_passed": sum(item["quality_gate"] == "PASS" for item in chunk_reports),
        "chunks_failed": sum(item["quality_gate"] == "FAIL" for item in chunk_reports),
        "pass_count": total_score["pass_count"], "fail_count": total_score["fail_count"],
        "inconclusive_count": total_score["inconclusive_count"],
        "not_applicable_count": total_score["not_applicable_count"],
        "scorable_criteria": total_score["scorable_count"],
        "pass_percentage": total_score["pass_percentage"], "signed_score": total_score["signed_score"],
        "raw_score": total_score["raw_score"], "overall_score": pass_pct,
        "scored_dimensions": scorecard["scored_dimensions"],
        "core_failures": scorecard["fail_count"],
        "core_inconclusive": scorecard["inconclusive_count"], "critical_failures": critical,
        "high_failures": high, "source_coverage_percentage": round(coverage_pct, 2) if coverage_pct is not None else None,
        "eligible_source_units": len(eligible_units), "covered_source_units": covered_count,
        "missing_source_units": len(missing_units), "pdf_docx_discrepancies": len(upstream_issues),
    }
    criteria_summary = []
    for criterion in _CRITERIA["criteria"]:
        results = [item for item in findings if item["criterion_id"] == criterion["id"]]
        criteria_summary.append({**criterion, **_criterion_score(results)})
    return {
        "evaluation_id": evaluation_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "COMPLETED",
        "quality_gate": gate,
        "quality_gate_thresholds": thresholds,
        "document_id": config.get("document_id") or doc_info.get("document_id") or doc_info.get("source_filename"),
        "chunking_job_id": config.get("chunking_job_id") or config.get("job_id"),
        "chunking_strategy": config.get("strategy") or config.get("chunking_strategy") or chunks[0].get("strategy"),
        "chunk_file_metadata": chunks[0].get("chunk_file_metadata", {}),
        "criteria_version": _CRITERIA["criteria_version"],
        "judge_application_version": __version__,
        "source_kind_for_alignment": source_kind,
        "summary": summary,
        "scorecard": scorecard,
        "action_plan": build_action_plan(gate, findings, missing_units, upstream_issues),
        "coverage": {
            "source_coverage_percentage": summary["source_coverage_percentage"],
            "eligible_source_units": len(eligible_units), "covered_source_units": covered_count,
            "unmatched_docx_units": [
                {"source_ref": unit["unit_id"], "section_path": unit["section_path"],
                 "source_excerpt": _truncate(unit["text"]),
                 "supported_by_pdf": any(_alignment(unit["text"], pdf_unit["text"])[0] >= 0.24
                                          for pdf_unit in evidence["pdf_units"])}
                for unit in missing_units[:200]
            ],
            "pdf_docx_upstream_issues": upstream_issues[:500],
            "sections_covered": len(covered_section_paths), "source_sections": len(source_section_paths),
        },
        "sections": section_reports,
        "chunks": chunk_reports,
        "findings": findings,
        "criteria_summary": criteria_summary,
        "inputs": {
            "pdflens_package_sha256": bundle.package_sha256,
            "chunks_sha256": chunks_sha256,
            "source_pdf_sha256": hashlib.sha256(bundle.pdf_bytes).hexdigest(),
            "source_docx_sha256": hashlib.sha256(bundle.docx_bytes).hexdigest() if bundle.docx_bytes else None,
            "source_pdf_member": bundle.pdf_name, "source_docx_member": bundle.docx_name,
            "package_members": bundle.package_members, "pdf_page_count": evidence["page_count"],
            "chunk_input_count": len(chunks),
        },
        "configuration": {key: value for key, value in config.items() if key != "entity_map"},
        "llm": llm_info,
        "criteria": _CRITERIA["criteria"],
        "evidence_notes": [
            "Converted DOCX is used as the primary source representation because it is the chunker input.",
            "Original PDF evidence is used to identify likely upstream PDF-to-DOCX omissions.",
            "A missing item from DOCX and PDF is not automatically attributed to chunking.",
            "No embedding model was selected; semantic matches are not asserted by this MVP.",
        ],
    }
