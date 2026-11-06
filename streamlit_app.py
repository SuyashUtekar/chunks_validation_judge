from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import streamlit as st

from chunking_strategy.sectionwise_chunking import chunk_docx_bytes
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
    "Review each chunk in plain language: what works, what may need fixing, what to improve, "
    "and which source passage supports the result. PDF conversion checks are shown separately."
)

with st.sidebar:
    st.subheader("Evaluation options")
    config_text = st.text_area(
        "Chunking configuration (optional JSON)",
        value='{"max_words": 800, "overlap_words": 80}',
        height=190,
        help="Set sectionwise chunk size and overlap, plus optional evaluation thresholds. The judge generates chunks from the DOCX inside the ZIP.",
    )
    st.caption(
        "All scored criteria require the configured on-prem LiteLLM endpoint. "
        "If the endpoint is unavailable, affected chunks show 'Could not evaluate'; the judge does not invent a result."
    )

package_upload = st.file_uploader(
    "PDFLens output ZIP", type=["zip"], key="package_zip",
    help="The ZIP must include the DOCX and may include metadata.json, assets, and the original PDF.",
)
source_pdf_upload = st.file_uploader("Original source PDF", type=["pdf"], key="source_pdf")
st.caption("The judge creates section-wise chunks from the DOCX inside the ZIP. If the ZIP also contains a PDF, it must match the PDF uploaded here.")

if package_upload:
    st.caption(f"PDFLens package: {package_upload.name} · {package_upload.size / (1024 * 1024):.1f} MiB")
if source_pdf_upload:
    st.caption(f"Source PDF: {source_pdf_upload.name} · {source_pdf_upload.size / (1024 * 1024):.1f} MiB")

run_evaluation = st.button(
    "Create chunks and evaluate",
    type="primary",
    disabled=not (package_upload and source_pdf_upload),
    width="stretch",
)

if run_evaluation:
    for key in ("judge_report", "judge_xlsx", "judge_json", "judge_chunks_json"):
        st.session_state.pop(key, None)
    try:
        package_bytes = package_upload.getvalue()
        source_pdf_bytes = source_pdf_upload.getvalue()
        package = load_pdflens_package(
            package_bytes, source_pdf_bytes, source_pdf_upload.name,
        )
        if not package.docx_bytes:
            raise InputValidationError("PDFLens ZIP must contain the DOCX used to generate the chunks.")
        config = parse_config(config_text)
        max_words = int(config.get("max_words", 800))
        overlap_words = int(config.get("overlap_words", 80))
        if overlap_words >= max_words:
            raise InputValidationError("overlap_words must be less than max_words for sectionwise chunking.")
        config.setdefault("strategy", "sectionwise")
        config["max_words"] = max_words
        config["overlap_words"] = overlap_words
        generated_chunks = chunk_docx_bytes(
            package.docx_bytes, package.docx_name or "document.docx",
            max_words=max_words, overlap_words=overlap_words,
        )
        chunks_bytes = (json.dumps(generated_chunks, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        chunks = normalize_chunks(chunks_bytes, "generated_sectionwise_chunks.json")
        logger.info(
            "sectionwise_chunks_generated package=%s docx=%s chunks=%s max_words=%s overlap_words=%s",
            Path(package_upload.name).name, package.docx_name, len(chunks), max_words, overlap_words,
        )
        with st.spinner(f"Generated {len(chunks)} chunks. Evaluating against the PDFLens source…"):
            report = evaluate(package, chunks, config, sha256(chunks_bytes))
            report["inputs"]["chunks_filename"] = "generated_sectionwise_chunks.json"
            report["inputs"]["chunks_generated_by_judge"] = True
            report["inputs"]["pdflens_package_filename"] = Path(package_upload.name).name
            workbook_bytes = build_report_xlsx(report)
            json_bytes = report_json_bytes(report)
        st.session_state["judge_report"] = report
        st.session_state["judge_xlsx"] = workbook_bytes
        st.session_state["judge_json"] = json_bytes
        st.session_state["judge_chunks_json"] = chunks_bytes
    except InputValidationError as exc:
        st.error(str(exc))
    except ValueError as exc:
        st.error(str(exc))
    except Exception as exc:
        logger.exception("streamlit_evaluation_failed error_type=%s", type(exc).__name__)
        st.error(f"Evaluation failed ({type(exc).__name__}). Check the application terminal logs.")

report: dict[str, Any] | None = st.session_state.get("judge_report")
if report:
    st.divider()
    summary = report["summary"]
    review_summary = report.get("review_summary", {})
    st.subheader("Judge result")
    decision = review_summary.get("decision", "Could not evaluate")
    if decision == "Looks good":
        st.success(decision)
    elif decision == "Fixes needed":
        st.error(decision)
    else:
        st.warning(decision)
    st.write(review_summary.get("message", "Review the chunk results below."))

    chunk_reports = report.get("chunks", [])
    result_columns = st.columns(5)
    result_columns[0].metric("Total chunks", review_summary.get("total_chunks", len(chunk_reports)))
    result_columns[1].metric("Chunks with problems", review_summary.get("chunks_needing_correction", 0))
    result_columns[2].metric("Chunks to verify", review_summary.get("chunks_needing_source_review", 0))
    result_columns[3].metric("Chunks looking good", review_summary.get("chunks_looks_good", 0))
    result_columns[4].metric("Could not evaluate", review_summary.get("chunks_not_evaluated", 0))
    st.caption("Each chunk is counted once using its displayed verdict. A source check is not a confirmed failure.")
    llm_info = report.get("llm", {})
    if not llm_info.get("configured"):
        st.warning("LiteLLM is not configured or unavailable. No LLM criteria were scored; configure it and rerun.")
    elif llm_info.get("failed_chunk_calls"):
        st.warning(
            f"LiteLLM was configured, but {llm_info.get('failed_chunk_calls')} of "
            f"{llm_info.get('calls_attempted', 0)} attempted chunk calls failed. "
            "Check the evaluator terminal log and the chunk marked 'Could not evaluate'."
        )
    with st.expander("Evaluation and scoring details"):
        st.caption(
            f"Evaluation `{report['evaluation_id']}` · Alignment source: {report['source_kind_for_alignment']} · "
            f"LLM configured: {'yes' if report['llm']['configured'] else 'no'} · "
            f"Calls succeeded: {report['llm'].get('calls_succeeded', 0)}/"
            f"{report['llm'].get('calls_attempted', 0)} · "
            f"Chunks without aligned source: {report['llm'].get('chunks_without_source_evidence', 0)} · "
            f"Invalid criterion results: {report['llm'].get('invalid_criterion_results', 0)}"
        )
        st.write(
            f"Scoring details: PASS = +1, FAIL = −1, unresolved = 0. "
            f"Rating: {summary.get('overall_score') if summary.get('overall_score') is not None else 'not available'}%. "
            f"Pass threshold: {report.get('quality_gate_thresholds', {}).get('pass_candidate_at', 85):g}%; "
            f"below {report.get('quality_gate_thresholds', {}).get('review_required_below', 65):g}% is a fail. "
            "PDF/DOCX coverage is diagnostic and does not change the chunking score."
        )
        if summary.get("unresolved_reasons"):
            st.markdown("**Why the judge could not verify some items**")
            st.dataframe([
                {"Reason": reason, "Items": count}
                for reason, count in summary["unresolved_reasons"].items()
            ], width="stretch", hide_index=True)

    generated_chunk_count = report.get("summary", {}).get("total_chunks", len(chunk_reports))
    st.caption(f"Sectionwise chunks created by the judge: {generated_chunk_count}")
    download_columns = st.columns(3)
    package_filename = report["inputs"].get("pdflens_package_filename", "document.zip")
    report_stem = Path(report.get("document_id") or Path(package_filename).stem).stem
    download_columns[0].download_button(
        "Download concise Excel report",
        data=st.session_state["judge_xlsx"],
        file_name=f"{report_stem}_chunk_quality_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        width="stretch",
    )
    download_columns[1].download_button(
        "Download concise JSON report",
        data=st.session_state["judge_json"],
        file_name=f"{report_stem}_chunk_quality_report.json",
        mime="application/json",
        width="stretch",
    )
    download_columns[2].download_button(
        "Download generated chunks JSON",
        data=st.session_state["judge_chunks_json"],
        file_name=f"{report_stem}_sectionwise_chunks.json",
        mime="application/json",
        width="stretch",
    )

    st.subheader("Chunk-by-chunk review")
    st.caption("For each chunk, review the strengths, any problem, the suggested improvement, and the cited source comparison.")
    chunk_reviews = report.get("chunk_reviews", [])
    if chunk_reviews:
        filter_options = ["Needs attention", "All chunks", "Needs a fix", "Check against source", "Looks good", "Could not evaluate"]
        default_filter = "Needs attention" if any(item["verdict"] in {"Needs a fix", "Check against source", "Could not evaluate"} for item in chunk_reviews) else "Looks good"
        selected_filter = st.selectbox("Show", filter_options, index=filter_options.index(default_filter))
        if selected_filter == "Needs attention":
            visible_reviews = [item for item in chunk_reviews if item["verdict"] != "Looks good"]
        elif selected_filter == "All chunks":
            visible_reviews = chunk_reviews
        else:
            visible_reviews = [item for item in chunk_reviews if item["verdict"] == selected_filter]

        action_labels = {
            "KEEP_AS_IS": "Keep as is", "EDIT_CHUNK_TEXT": "Edit chunk text",
            "MOVE_TO_SECTION": "Move to the correct section", "SPLIT_CHUNK": "Split this chunk",
            "ADJUST_BOUNDARY": "Adjust the chunk boundary", "ADD_CONTEXT": "Add necessary context",
            "CHECK_SOURCE": "Check the source passage", "CHECK_PDFLENS": "Review PDF-to-DOCX extraction",
            "RETRY_EVALUATION": "Fix the judge error and rerun",
        }
        for index, item in enumerate(visible_reviews):
            section = " > ".join(item.get("section_path") or []) or "No section path"
            pages = ", ".join(map(str, item.get("page_numbers") or []))
            header = f"{item['verdict']} · {item['chunk_id']} · {section}"
            if pages:
                header += f" · page(s) {pages}"
            with st.expander(header, expanded=index == 0 and selected_filter == "Needs attention"):
                st.write(item.get("summary", ""))
                if item.get("strengths"):
                    st.markdown("**What works well**")
                    for strength in item["strengths"]:
                        st.markdown(f"- **{strength['criterion']}:** {strength['explanation']}")
                        refs = ", ".join(strength.get("source_refs") or []) or "Source evidence cited"
                        st.caption(f"Source reference: {refs}")
                        source_col, chunk_col = st.columns(2)
                        with source_col:
                            st.markdown("**Source passage**")
                            st.info(strength.get("source_excerpt") or "No source excerpt was retained.")
                        with chunk_col:
                            st.markdown("**Chunk text**")
                            st.info(strength.get("chunk_excerpt") or item.get("chunk_text") or "No chunk excerpt was retained.")
                else:
                    st.markdown("**What works well**")
                    st.caption("No specific strength was confirmed from the available evidence.")

                for heading, issue_key in (("Problems found", "problems"), ("Please verify", "checks_to_verify")):
                    issues = item.get(issue_key, [])
                    if not issues:
                        continue
                    st.markdown(f"**{heading}**")
                    for issue in issues:
                        st.markdown(f"*{issue['criterion']}* — {issue['explanation']}")
                        refs = ", ".join(issue.get("source_refs") or []) or "No source passage could be cited"
                        st.caption(f"Source reference: {refs}")
                        source_col, chunk_col = st.columns(2)
                        with source_col:
                            st.markdown("**Source passage**")
                            st.info(issue.get("source_excerpt") or "No aligned source passage was available.")
                        with chunk_col:
                            st.markdown("**Chunk text**")
                            st.info(issue.get("chunk_excerpt") or item.get("chunk_text") or "No chunk text was available.")
                        if issue.get("improvement"):
                            st.markdown(f"**Suggested improvement:** {issue['improvement']}")

                if item.get("could_not_evaluate"):
                    st.markdown("**The judge could not assess this chunk**")
                    for issue in item["could_not_evaluate"]:
                        st.warning(f"{issue['criterion']}: {issue['explanation']}")

                if item.get("improvements"):
                    st.markdown("**Recommended next step**")
                    for improvement in item["improvements"]:
                        st.write(improvement)
                action = action_labels.get(item.get("recommended_action"), "Check the cited evidence")
                st.caption(f"Suggested action: {action}")
                st.markdown("**Five-question checklist**")
                st.dataframe(item.get("criteria", []), width="stretch", hide_index=True)
                if item["verdict"] == "Looks good":
                    st.markdown("**Chunk text**")
                    st.text(item.get("chunk_text") or "No chunk text was available.")
    else:
        st.warning("No chunk-level review was generated. Check LiteLLM and source alignment before accepting the result.")

    with st.expander("Scoring detail by criterion"):
        st.caption("These five checks determine the score. The chunk review queue above is the recommended place to start.")
        scorecard = report.get("scorecard", {})
        if scorecard.get("dimensions"):
            st.dataframe([
                {"Criterion": item["dimension"],
                 "Pass rate": f"{item['pass_percentage']:.1f}%" if item["pass_percentage"] is not None else "Not scored",
                 "Passed": item["pass_count"], "Failed": item["fail_count"],
                 "Needs source review": item["inconclusive_count"],
                 "Result": {"PASS": "No failures", "FAIL": "All assessed checks failed", "MIXED": "Some checks failed", "INCONCLUSIVE": "Needs evidence"}.get(item["status"], item["status"]),
                 "What it checks": item["what_it_checks"]}
                for item in scorecard["dimensions"]
            ], width="stretch", hide_index=True)
        else:
            st.write("No criterion outcomes were available. Check LiteLLM and source alignment before accepting the result.")

    with st.expander("How to read these results"):
        st.markdown(
            "- **Looks good:** the supplied evidence shows no clear chunking issue.\n"
            "- **Needs a fix:** the source passage demonstrates a specific problem in the chunk.\n"
            "- **Check against source:** evidence is missing or uncertain; this is not a confirmed failure.\n"
            "- **Could not evaluate:** the judge did not complete its assessment.\n"
            "- PDF-to-DOCX extraction checks are separate from chunking findings."
        )

    tabs = st.tabs(["Chunk list", "Criterion evidence", "Sections", "Source diagnostics"])
    with tabs[0]:
        review_by_id = {item["chunk_id"]: item for item in chunk_reviews}
        chunk_rows = [{
            "Chunk": item["chunk_id"],
            "Section": " > ".join(item.get("section_path") or []) or "No section path",
            "Verdict": review_by_id.get(item["chunk_id"], {}).get("verdict", "Could not evaluate"),
            "Summary": review_by_id.get(item["chunk_id"], {}).get("summary", ""),
            "Suggested action": {
                "KEEP_AS_IS": "Keep as is", "EDIT_CHUNK_TEXT": "Edit chunk text",
                "MOVE_TO_SECTION": "Move to the correct section", "SPLIT_CHUNK": "Split this chunk",
                "ADJUST_BOUNDARY": "Adjust the chunk boundary", "ADD_CONTEXT": "Add necessary context",
                "CHECK_SOURCE": "Check the source passage", "RETRY_EVALUATION": "Fix the judge error and rerun",
            }.get(review_by_id.get(item["chunk_id"], {}).get("recommended_action"), "Review evidence"),
        } for item in report["chunks"]]
        st.dataframe(chunk_rows, width="stretch", hide_index=True)
    with tabs[1]:
        outcome_labels = {"FAIL": "Needs a fix", "INCONCLUSIVE": "Check against source", "PASS": "Looks good"}
        selected_outcomes = st.multiselect(
            "Show scored results",
            options=list(outcome_labels.values()),
            default=["Needs a fix", "Check against source"],
        )
        scored_ids = {criterion_id for dimension in report.get("scorecard", {}).get("dimensions", [])
                      for criterion_id in dimension.get("criteria", [])}
        visible_findings = [{
            "Criterion": item.get("criterion"),
            "Chunk": item.get("chunk_id") or "Document",
            "Result": outcome_labels.get(item.get("outcome"), item.get("outcome")),
            "Why": item.get("explanation"),
            "Source evidence": item.get("source_excerpt"),
            "Chunk evidence": item.get("chunk_excerpt"),
            "Suggested improvement": item.get("improvement"),
        } for item in report["findings"]
          if item.get("criterion_id") in scored_ids
          and outcome_labels.get(item.get("outcome")) in selected_outcomes]
        st.dataframe(visible_findings, width="stretch", hide_index=True)
    with tabs[2]:
        grouped_sections: dict[str, list[dict[str, Any]]] = {}
        for item in chunk_reviews:
            section_name = " > ".join(item.get("section_path") or []) or "No section path"
            grouped_sections.setdefault(section_name, []).append(item)
        section_rows = [{
            "Section": name,
            "Chunks": len(items),
            "Needs a fix": sum(item["verdict"] == "Needs a fix" for item in items),
            "Check against source": sum(item["verdict"] == "Check against source" for item in items),
            "Looks good": sum(item["verdict"] == "Looks good" for item in items),
            "Could not evaluate": sum(item["verdict"] == "Could not evaluate" for item in items),
        } for name, items in grouped_sections.items()]
        st.dataframe(section_rows, width="stretch", hide_index=True)
    with tabs[3]:
        coverage = report["coverage"]
        st.caption("These source coverage and PDF-to-DOCX comparisons are diagnostics only; they do not affect the LLM criteria score.")
        st.write(
            f"DOCX source coverage diagnostic: {coverage['covered_source_units']} of "
            f"{coverage['eligible_source_units']} source units matched to chunks."
        )
        if coverage["unmatched_docx_units"]:
            st.markdown("**DOCX source units not found in chunks**")
            st.dataframe(coverage["unmatched_docx_units"], width="stretch", hide_index=True)
        if coverage["pdf_docx_upstream_issues"]:
            st.markdown("**Possible upstream PDF-to-DOCX omissions**")
            st.dataframe(coverage["pdf_docx_upstream_issues"], width="stretch", hide_index=True)

    st.info("Use the chunk-by-chunk verdict and cited passages as the primary result. Source extraction diagnostics are separate.")
