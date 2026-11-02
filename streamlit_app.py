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

package_upload = st.file_uploader("PDFLens output package", type=["zip"], key="package_zip")
chunks_upload = st.file_uploader("Section-wise chunks JSON", type=["json"], key="chunks_json")

if package_upload:
    st.caption(f"PDFLens package: {package_upload.name} · {package_upload.size / (1024 * 1024):.1f} MiB")
if chunks_upload:
    st.caption(f"Chunks: {chunks_upload.name} · {chunks_upload.size / 1024:.1f} KiB")

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
        package = load_pdflens_package(package_bytes)
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

    columns = st.columns(5)
    columns[0].metric("Chunks", summary["total_chunks"])
    columns[1].metric("Pass rate", f"{summary['pass_percentage']:.2f}%" if summary["pass_percentage"] is not None else "—")
    columns[2].metric("Failures", summary["fail_count"])
    columns[3].metric("Inconclusive", summary["inconclusive_count"])
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
        "Download Excel report",
        data=st.session_state["judge_xlsx"],
        file_name=f"{report_stem}_chunk_quality_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )
    download_columns[1].download_button(
        "Download JSON report",
        data=st.session_state["judge_json"],
        file_name=f"{report_stem}_chunk_quality_report.json",
        mime="application/json",
        use_container_width=True,
    )

    tabs = st.tabs(["Chunks", "Findings", "Sections", "Coverage"])
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
