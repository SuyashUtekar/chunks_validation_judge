from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import streamlit as st

from judge_app.evaluation import evaluate
from judge_app.ingestion import (
    InputValidationError,
    load_pdflens_package,
    normalize_chunks,
    parse_config,
    sha256,
)
from judge_app.reporting import build_report_xlsx, report_json_bytes

logger = logging.getLogger("chunk_quality_judge.streamlit")

st.set_page_config(page_title="Section-wise Chunk Quality Judge", page_icon="📑", layout="wide")
st.title("Section-wise Chunk Quality Judge")
st.write(
    "Upload a PDFLens output ZIP and the section-wise chunks JSON. "
    "The judge checks chunk coverage and traceability, highlights potential upstream "
    "PDF-to-DOCX omissions, and prepares an Excel report."
)

with st.sidebar:
    st.subheader("Evaluation options")
    config_text = st.text_area(
        "Chunking configuration (optional JSON)",
        value="{}",
        height=190,
        help=("Supply values such as strategy, max_words, overlap_words, required metadata, "
              "entity map, and provisional quality thresholds."),
    )
    st.caption(
        "Contextual checks use the configured on-prem LLM, if available. "
        "Without it, those checks are marked INCONCLUSIVE."
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
        logger.exception("Streamlit evaluation failed error_type=%s", type(exc).__name__)
        st.error(f"Evaluation failed ({type(exc).__name__}). Check the application terminal logs.")

report: dict[str, Any] | None = st.session_state.get("judge_report")
if report:
    st.divider()
    summary = report["summary"]
    st.subheader("Evaluation summary")
    gate = report["quality_gate"]
    if gate == "PASS":
        st.success(f"Quality gate: {gate}")
    elif gate == "FAIL":
        st.error(f"Quality gate: {gate}")
    else:
        st.warning(f"Quality gate: {gate}")
    st.info(report.get("action_plan", {}).get("summary", "Review the findings and evidence below."))

    columns = st.columns(5)
    columns[0].metric("Chunks", summary["total_chunks"])
    columns[1].metric("Overall score", f"{summary['overall_score']:.2f}%" if summary.get("overall_score") is not None else "—")
    columns[2].metric("Scored failures", summary.get("core_failures", 0))
    columns[3].metric("Unresolved scored checks", summary.get("core_inconclusive", 0))
    columns[4].metric(
        "DOCX source coverage",
        f"{summary['source_coverage_percentage']:.2f}%"
        if summary["source_coverage_percentage"] is not None else "—",
    )

    st.caption(
        f"Evaluation `{report['evaluation_id']}` · "
        f"Alignment source: {report['source_kind_for_alignment']} · "
        f"LLM configured: {'yes' if report['llm']['configured'] else 'no'}"
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

    st.caption("Downloads contain a compact overview, five-point scorecard, next steps, and up to 40 evidence rows. The detailed findings remain available in the app below.")
    st.subheader("Five-point scorecard")
    st.caption("The overall score is the average of the scored dimensions below. Detailed checks remain available as evidence and are not each counted as separate top-level points.")
    scorecard = report.get("scorecard", {})
    if scorecard.get("dimensions"):
        st.dataframe([
            {"Dimension": item["dimension"], "Score": f"{item['pass_percentage']:.1f}%" if item["pass_percentage"] is not None else "Not scored",
             "Result": item["status"], "What it checks": item["what_it_checks"]}
            for item in scorecard["dimensions"]
        ], use_container_width=True, hide_index=True)
    else:
        st.write("No scorecard dimensions had enough evidence to score.")

    st.subheader("What to do next")
    action_items = report.get("action_plan", {}).get("actions", [])
    if action_items:
        for index, action in enumerate(action_items, start=1):
            st.markdown(f"**{index}. {action['priority']} priority — {action['title']}**")
            st.write(action["what_to_do"])
            details = []
            if action.get("chunk_ids"):
                details.append("Chunks: " + ", ".join(action["chunk_ids"][:12]))
            if action.get("criterion_ids"):
                details.append("Checks: " + ", ".join(action["criterion_ids"]))
            if action.get("source_refs"):
                details.append("Source refs: " + ", ".join(action["source_refs"][:8]))
            if action.get("pages"):
                details.append("Pages: " + ", ".join(str(page) for page in action["pages"]))
            if details:
                st.caption(" · ".join(details))
    else:
        st.write("No corrective actions were identified from the available evidence.")

    with st.expander("How to read these results"):
        st.markdown(
            "- **PASS:** met the current provisional thresholds.\n"
            "- **REVIEW_REQUIRED:** inspect the listed failures and unresolved checks.\n"
            "- **FAIL:** fix critical findings or results below the configured review boundary.\n"
            "- **INCONCLUSIVE:** evidence was insufficient; this does not by itself mean the chunk is wrong.\n"
            "- If PDF text is missing from the DOCX, investigate PDFLens conversion before changing chunking."
        )

    tabs = st.tabs(["Chunks", "Findings & evidence", "Sections", "Coverage"])
    with tabs[0]:
        st.dataframe(report["chunks"], use_container_width=True, hide_index=True)
    with tabs[1]:
        selected_outcomes = st.multiselect(
            "Filter outcomes",
            options=["FAIL", "INCONCLUSIVE", "PASS", "N/A"],
            default=["FAIL", "INCONCLUSIVE"],
        )
        visible_findings = [item for item in report["findings"] if item["outcome"] in selected_outcomes]
        st.dataframe(visible_findings, use_container_width=True, hide_index=True)
    with tabs[2]:
        st.dataframe(report["sections"], use_container_width=True, hide_index=True)
    with tabs[3]:
        coverage = report["coverage"]
        st.write(
            f"Covered {coverage['covered_source_units']} of "
            f"{coverage['eligible_source_units']} eligible DOCX source units."
        )
        if coverage["unmatched_docx_units"]:
            st.markdown("**DOCX source units not found in chunks**")
            st.dataframe(coverage["unmatched_docx_units"], use_container_width=True, hide_index=True)
        if coverage["pdf_docx_upstream_issues"]:
            st.markdown("**Possible upstream PDF-to-DOCX omissions**")
            st.dataframe(coverage["pdf_docx_upstream_issues"], use_container_width=True, hide_index=True)

    st.info(
        "The criteria set and score thresholds are provisional. Review INCONCLUSIVE findings "
        "and evidence before treating the quality gate as an acceptance decision."
    )
