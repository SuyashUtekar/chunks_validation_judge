from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import streamlit as st

from judge_app.config import settings
from judge_app.evaluation import evaluate
from judge_app.ingestion import (
    InputValidationError,
    load_pdflens_package,
    normalize_chunks,
    parse_config,
    sha256,
)
from judge_app.reporting import build_report_xlsx, report_json_bytes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("chunk_quality_judge.streamlit")
logger.info(
    "judge_settings_loaded llm_url_configured=%s llm_model_configured=%s "
    "llm_api_key_configured=%s llm_json_mode=%s llm_timeout_seconds=%s",
    bool(settings.llm_base_url), bool(settings.llm_model), bool(settings.llm_api_key),
    settings.llm_json_mode, settings.llm_timeout_seconds,
)

st.set_page_config(page_title="Section-wise Chunk Quality Judge", page_icon="📑", layout="wide")
st.title("Section-wise Chunk Quality Judge")
st.write(
    "Compare section-wise chunks with the DOCX that produced them. LiteLLM judges five criteria: "
    "source accuracy, condition/negation preservation, section assignment, topic coherence, and chunk boundaries. The score uses those criteria only; "
    "PDF conversion and structural measurements are shown separately as diagnostics."
)

with st.sidebar:
    st.subheader("Evaluation options")
    config_text = st.text_area(
        "Chunking configuration (optional JSON)",
        value="{}",
        height=190,
        help="Optionally set the pass threshold and review boundary. These thresholds change the decision label, not the LLM criterion score.",
    )
    st.caption(
        "All scored criteria require the configured on-prem LiteLLM endpoint. "
        "If the endpoint is unavailable, the judge reports 'Could not assess' and does not calculate a fallback score."
    )

package_upload = st.file_uploader("PDFLens output ZIP", type=["zip"], key="package_zip",
                                 help="The ZIP may contain document.docx, metadata.json, and assets. If it does not contain the original PDF, upload that PDF below.")
source_pdf_upload = st.file_uploader("Original source PDF (if not inside ZIP)", type=["pdf"], key="source_pdf")
chunks_upload = st.file_uploader("Section-wise chunks JSON", type=["json"], key="chunks_json")

if package_upload:
    st.caption(f"PDFLens package: {package_upload.name} · {package_upload.size / (1024 * 1024):.1f} MiB")
if chunks_upload:
    st.caption(f"Chunks: {chunks_upload.name} · {chunks_upload.size / 1024:.1f} KiB")
if source_pdf_upload:
    st.caption(f"Source PDF: {source_pdf_upload.name} · {source_pdf_upload.size / (1024 * 1024):.1f} MiB")

run_evaluation = st.button(
    "Evaluate chunks",
    type="primary",
    disabled=not (package_upload and chunks_upload),
    use_container_width=True,
)

if run_evaluation:
    for key in ("judge_report", "judge_xlsx", "judge_json"):
        st.session_state.pop(key, None)
    try:
        package_bytes = package_upload.getvalue()
        chunks_bytes = chunks_upload.getvalue()
        package = load_pdflens_package(
            package_bytes,
            source_pdf_upload.getvalue() if source_pdf_upload else None,
            source_pdf_upload.name if source_pdf_upload else None,
        )
        chunks = normalize_chunks(chunks_bytes, chunks_upload.name)
        config = parse_config(config_text)
        with st.spinner(f"Evaluating {len(chunks)} chunks against the PDFLens source…"):
            report = evaluate(package, chunks, config, sha256(chunks_bytes))
            report["inputs"]["chunks_filename"] = Path(chunks_upload.name).name
            report["inputs"]["pdflens_package_filename"] = Path(package_upload.name).name
            workbook_bytes = build_report_xlsx(report)
            json_bytes = report_json_bytes(report)
        st.session_state["judge_report"] = report
        st.session_state["judge_xlsx"] = workbook_bytes
        st.session_state["judge_json"] = json_bytes
    except InputValidationError as exc:
        st.error(str(exc))
    except Exception as exc:
        logger.exception("streamlit_evaluation_failed error_type=%s", type(exc).__name__)
        st.error(f"Evaluation failed ({type(exc).__name__}). Check the application terminal logs.")

report: dict[str, Any] | None = st.session_state.get("judge_report")
if report:
    st.divider()
    summary = report["summary"]
    st.subheader("Judge result")
    gate = report["quality_gate"]
    if gate == "PASS":
        st.success("Meets the current pass threshold")
    elif gate == "FAIL":
        st.error("Needs correction")
    else:
        st.warning("Needs review")
    st.write(report.get("action_plan", {}).get("summary", "Review the criteria and evidence below."))

    denominator = summary.get("score_denominator", 0)
    score_value = summary.get("overall_score")
    score_text = f"{score_value:.1f}%" if score_value is not None else "Not available"
    result_columns = st.columns(5)
    result_columns[0].metric("Criteria rating", score_text,
                             help="Converted from the signed criterion points. Unresolved checks are excluded from the rating.")
    net_points = summary.get("net_score_points", 0)
    max_points = summary.get("max_score_points", denominator)
    result_columns[1].metric("Net points", f"{net_points:+d} / {max_points:+d}")
    result_columns[2].metric("Checks passed", summary.get("core_passes", 0))
    result_columns[3].metric("Checks failed", summary.get("core_failures", 0))
    result_columns[4].metric("Could not assess", summary.get("core_inconclusive", 0))
    st.caption(f"Chunks in file: {summary.get('total_chunks', 0)}.")
    st.caption(
        f"Each criterion result is PASS=+1, FAIL=−1, and could not assess=0. "
        f"The rating is (net points ÷ conclusive checks + 1) ÷ 2 × 100. "
        f"Pass threshold: {report.get('quality_gate_thresholds', {}).get('pass_candidate_at', 85):g}%; "
        f"below {report.get('quality_gate_thresholds', {}).get('review_required_below', 65):g}% is a fail. "
        "Unresolved checks are shown separately and are not treated as failures."
    )
    llm_info = report.get("llm", {})
    if not llm_info.get("configured"):
        st.warning("LiteLLM is not configured or unavailable. No LLM criteria were scored; configure it and rerun.")
    elif llm_info.get("failed_chunk_calls"):
        st.warning(
            f"LiteLLM was configured, but {llm_info.get('failed_chunk_calls')} of "
            f"{llm_info.get('calls_attempted', 0)} attempted chunk calls failed. "
            "Check the evaluator terminal log and the 'Could not assess' reason below."
        )
    if summary.get("unresolved_reasons"):
        st.markdown("**Why some checks could not be assessed**")
        st.dataframe([
            {"Reason": reason, "Checks": count}
            for reason, count in summary["unresolved_reasons"].items()
        ], use_container_width=True, hide_index=True)

    st.caption(
        f"Evaluation `{report['evaluation_id']}` · "
        f"Alignment source: {report['source_kind_for_alignment']} · "
        f"LLM configured: {'yes' if report['llm']['configured'] else 'no'} · "
        f"calls succeeded: {report['llm'].get('calls_succeeded', 0)}/"
        f"{report['llm'].get('calls_attempted', 0)} attempted · "
        f"chunks without aligned source: {report['llm'].get('chunks_without_source_evidence', 0)} · "
        f"invalid criterion results: {report['llm'].get('invalid_criterion_results', 0)}"
    )

    download_columns = st.columns(2)
    package_filename = report["inputs"].get("pdflens_package_filename", "document.zip")
    report_stem = Path(report.get("document_id") or Path(package_filename).stem).stem
    download_columns[0].download_button(
        "Download concise Excel report",
        data=st.session_state["judge_xlsx"],
        file_name=f"{report_stem}_chunk_quality_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )
    download_columns[1].download_button(
        "Download concise JSON report",
        data=st.session_state["judge_json"],
        file_name=f"{report_stem}_chunk_quality_report.json",
        mime="application/json",
        use_container_width=True,
    )

    st.caption("Reports show the five scored LLM criteria first. PDF and deterministic measurements are separated as diagnostics and do not change the score.")
    st.subheader("Scored criteria")
    st.caption("Each row is one criterion. Its point total sums +1 for PASS, −1 for FAIL, and 0 for unresolved checks.")
    scorecard = report.get("scorecard", {})
    if scorecard.get("dimensions"):
        st.dataframe([
            {"Criterion": item["dimension"],
             "Pass rate": f"{item['pass_percentage']:.1f}%" if item["pass_percentage"] is not None else "Not scored",
             "Net points": f"{item['score_points']:+d} / {item['max_points']:+d}",
             "Passed": item["pass_count"], "Failed": item["fail_count"],
             "Could not assess": item["inconclusive_count"],
             "Result": {"PASS": "Meets criterion", "FAIL": "Needs correction", "MIXED": "Some checks failed", "INCONCLUSIVE": "Needs evidence"}.get(item["status"], item["status"]),
             "What it checks": item["what_it_checks"]}
            for item in scorecard["dimensions"]
        ], use_container_width=True, hide_index=True)
    else:
        st.write("No LLM criterion outcomes were available to score. Configure LiteLLM and rerun.")

    st.subheader("What to do next")
    action_items = report.get("action_plan", {}).get("actions", [])
    if action_items:
        for index, action in enumerate(action_items, start=1):
            st.markdown(f"**{index}. {action['priority']} priority — {action['title']}**")
            st.write(action["what_to_do"])
            details = []
            if action.get("affected_chunk_count"):
                details.append(f"Affected chunks: {action['affected_chunk_count']}")
            if action.get("chunk_ids"):
                samples = ", ".join(action["chunk_ids"][:3])
                more = action.get("more_chunk_ids_count", 0)
                details.append(f"Examples: {samples}" + (f" (+{more} more)" if more else ""))
            if action.get("criterion_ids"):
                details.append("Checks: " + ", ".join(action["criterion_ids"]))
            if action.get("source_refs"):
                refs = ", ".join(action["source_refs"][:5])
                more = action.get("more_source_refs_count", 0)
                details.append("Source refs: " + refs + (f" (+{more} more)" if more else ""))
            if action.get("pages"):
                details.append("Pages: " + ", ".join(str(page) for page in action["pages"][:8]))
            if details:
                st.caption(" · ".join(details))
    else:
        st.write("No corrective actions were identified from the available evidence.")

    with st.expander("How to read these results"):
        st.markdown(
            "- **Meets threshold:** score is at or above the pass threshold and no scored check is unresolved.\n"
            "- **Needs review:** score is in the review band, or the LLM could not assess some checks.\n"
            "- **Needs correction:** score is below the review boundary or a critical scored issue was found.\n"
            "- **Could not assess:** evidence was insufficient; this is not counted as a failure.\n"
            "- Source coverage and PDF-to-DOCX comparisons appear under diagnostics and do not change this score."
        )

    tabs = st.tabs(["Chunks", "Scored evidence", "Sections", "Source diagnostics (not scored)"])
    with tabs[0]:
        chunk_rows = [{
            "Chunk": item["chunk_id"],
            "Section": " > ".join(item.get("section_path") or []) or "No section path",
            "Criteria rating": f"{item['criteria_score_percent']:.1f}%" if item.get("criteria_score_percent") is not None else "Not scored",
            "Net points": f"{item.get('criteria_net_points', 0):+d} / {item.get('criteria_max_points', 0):+d}",
            "Passed": item.get("criteria_passed", 0),
            "Failed": item.get("criteria_failed", 0),
            "Could not assess": item.get("criteria_unresolved", 0),
            "Source refs": ", ".join(item.get("aligned_source_refs") or []),
        } for item in report["chunks"]]
        st.dataframe(chunk_rows, use_container_width=True, hide_index=True)
    with tabs[1]:
        outcome_labels = {"FAIL": "Needs correction", "INCONCLUSIVE": "Could not assess", "PASS": "Meets criterion"}
        selected_outcomes = st.multiselect(
            "Show scored results",
            options=list(outcome_labels.values()),
            default=["Needs correction", "Could not assess"],
        )
        scored_ids = {criterion_id for dimension in report.get("scorecard", {}).get("dimensions", [])
                      for criterion_id in dimension.get("criteria", [])}
        visible_findings = [{
            "Criterion": item.get("criterion"),
            "Chunk": item.get("chunk_id") or "Document",
            "Result": outcome_labels.get(item.get("outcome"), item.get("outcome")),
            "Score": f"{item.get('score_points', 0):+d}",
            "Why": item.get("explanation"),
            "Source evidence": item.get("source_excerpt"),
            "Chunk evidence": item.get("chunk_excerpt"),
            "Confidence": item.get("confidence"),
        } for item in report["findings"]
          if item.get("criterion_id") in scored_ids
          and outcome_labels.get(item.get("outcome")) in selected_outcomes]
        st.dataframe(visible_findings, use_container_width=True, hide_index=True)
    with tabs[2]:
        section_rows = [{
            "Section": " > ".join(item.get("section_path") or []),
            "Chunks": item.get("chunk_count", 0),
            "Criteria rating": f"{item['criteria_score_percent']:.1f}%" if item.get("criteria_score_percent") is not None else "Not scored",
            "Net points": f"{item.get('criteria_net_points', 0):+d} / {item.get('criteria_max_points', 0):+d}",
            "Passed": item.get("criteria_passed", 0),
            "Failed": item.get("criteria_failed", 0),
            "Could not assess": item.get("criteria_unresolved", 0),
        } for item in report["sections"]]
        st.dataframe(section_rows, use_container_width=True, hide_index=True)
    with tabs[3]:
        coverage = report["coverage"]
        st.caption("These source coverage and PDF-to-DOCX comparisons are diagnostics only; they do not affect the LLM criteria score.")
        st.write(
            f"DOCX source coverage diagnostic: {coverage['covered_source_units']} of "
            f"{coverage['eligible_source_units']} source units matched to chunks."
        )
        if coverage["unmatched_docx_units"]:
            st.markdown("**DOCX source units not found in chunks**")
            st.dataframe(coverage["unmatched_docx_units"], use_container_width=True, hide_index=True)
        if coverage["pdf_docx_upstream_issues"]:
            st.markdown("**Possible upstream PDF-to-DOCX omissions**")
            st.dataframe(coverage["pdf_docx_upstream_issues"], use_container_width=True, hide_index=True)

    st.info("The five criterion scores determine the result. Review any 'Could not assess' evidence before accepting the result; thresholds are provisional.")
