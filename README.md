# Section-wise Chunk Quality Judge

A standalone local FastAPI application that evaluates section-wise chunks against a PDFLens output package and creates an evidence-backed Excel report. It is separate from the PDFLens application.

This is the first deterministic MVP from the implementation plan. It performs artifact validation, DOCX/PDF evidence extraction, section-aware chunk normalization, source alignment, rule-based checks, scoring, and Excel/JSON reporting. Contextual criteria are evaluated through an optional OpenAI-compatible **on-prem** model endpoint. Without that endpoint, those criteria are recorded as `INCONCLUSIVE`, never as `PASS`.

## Inputs

- A PDFLens ZIP containing `document.docx`, `metadata.json`, and any assets, plus the original source PDF. The PDF may be inside the ZIP or uploaded separately; if both are provided, their SHA-256 hashes must match. If a `manifest.json` exists, listed hashes are checked.
- Chunk records as JSON, XLSX, or CSV. The current sectionwise chunking output is supported:
  - JSON object containing a `chunks` array with `chunk_id`, `text`, and `section_path`.
  - XLSX/CSV columns such as `chunk_id`, `chunk_text`.
- Optional JSON chunking configuration, including strategy, max size, overlap, required metadata fields, entity map, and provisional score thresholds.

Example config:

```json
{
  "document_id": "sample-rbi-doc",
  "chunking_job_id": "run-2026-001",
  "strategy": "sectionwise",
  "max_words": 800,
  "overlap_words": 80,
  "required_metadata_fields": [],
  "source_unit_coverage_pass_threshold": 85,
  "pass_candidate_at": 85,
  "review_required_below": 65
}
```

No page/section/entity fields are assumed mandatory when the chunk source does not provide them; their findings become `INCONCLUSIVE` or `N/A` as appropriate.

## Run locally

From this folder:

```sh
uv sync
uv run uvicorn judge_app.main:app --reload --host 127.0.0.1 --port 8010
```

Open `http://127.0.0.1:8010/docs` for the upload form. Or submit the PDFLens ZIP, original PDF (when it is not inside the ZIP), and generated chunks:

```sh
curl -f -X POST http://127.0.0.1:8010/api/v1/chunk-evaluations \
  -F 'pdflens_package=@/path/to/pdflens-output.zip' \
  -F 'source_pdf=@/path/to/original.pdf' \
  -F 'chunks_file=@/path/to/chunks.xlsx' \
  -F 'chunking_config={"strategy":"sectionwise","max_words":800,"overlap_words":80}'
```

The synchronous MVP responds with an evaluation ID and URLs for the JSON report and Excel workbook. Reports are stored under `data/evaluations/<evaluation_id>/`; input PDF/DOCX/chunk files are not persisted. The default storage directory can be changed with `JUDGE_DATA_DIR`.

## Streamlit interface

The Streamlit interface takes the PDFLens output ZIP, a chunks JSON file, and the original PDF separately when the ZIP does not contain it. It displays the summary and findings, and provides downloadable Excel and JSON reports. Start it from this folder:

```sh
uv run streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501
```

Or run `make run-ui`. Open `http://127.0.0.1:8501`. The optional configuration JSON is in the sidebar. The interface holds generated reports in the current Streamlit session for download; it does not save the uploaded files.

## Reports

Downloads are intentionally concise. The JSON report contains the decision, source/chunk summary, five-point scorecard, grouped next steps, and up to 40 evidence-backed FAIL/INCONCLUSIVE rows. The Excel workbook has four sheets: **Overview**, **Scorecard**, **Next Steps**, and **Evidence to Review**. Longer diagnostics remain in the live app; they are not duplicated into the default downloads.

The initial criteria version is marked **proposed**. The judge reports five primary dimensions: content coverage, content fidelity, section placement, chunk coherence/usefulness, and traceability. Each dimension is supported by cited detailed checks; the detailed checks are evidence, not separate top-level score points. The overall score is the average of dimensions with enough evidence to score. Current provisional defaults are 85% for a pass candidate and 65% for the review boundary; these are not calibrated release thresholds. Critical failures still override the score. `N/A` and `INCONCLUSIVE` are reported separately.

## Optional on-prem LLM

No public model service is configured by default. LiteLLM Proxy is supported through its OpenAI-compatible Chat Completions API. Configure the proxy base URL and a model alias enabled in LiteLLM before starting the app:

```sh
export JUDGE_LLM_BASE_URL='http://127.0.0.1:4000/v1'
export JUDGE_LLM_MODEL='your-litellm-model-alias'
export JUDGE_LLM_API_KEY='your-litellm-virtual-key'
```

`JUDGE_LLM_BASE_URL` can be the proxy root (for example `http://127.0.0.1:4000`) or its `/v1` base URL; the app resolves either to the chat-completions route. `JUDGE_LLM_MODEL` must be the LiteLLM model alias available to that key. Set `JUDGE_LLM_JSON_MODE=false` if the selected LiteLLM model or deployment does not support JSON response mode. The evaluator sends compact chunk/source evidence, not the full PDF or full DOCX. The balanced, versioned rubric is in `prompts/chunk_judge_v4.txt`; it tolerates coherent adjacent subsections in one chunk while preserving strict checks for material content errors, and reports record the prompt version. It includes five concise calibration examples for related versus unrelated section grouping, upstream omissions, insufficient evidence, and a provable condition reversal. Configure `JUDGE_LLM_ALLOW_REMOTE=true` only when the endpoint has been reviewed and approved as internal. The default host check permits loopback and private-network endpoints. LLM responses are schema-checked; malformed, unsupported, or ungrounded results become `INCONCLUSIVE` and require review.

## MVP boundaries

- Evaluation currently runs synchronously; large-document queueing, database persistence, object storage, authentication, and human-review writeback are later plan phases.
- Text alignment is deterministic and lexical. There is no embedding model yet; lexical match scores are candidate alignment evidence, not semantic proof.
- DOCX is the primary representation consumed by chunking. PDF text is used to flag possible upstream PDF-to-DOCX omissions.
- Some checks need page references, offsets, entity maps, or an approved local LLM. Missing evidence is surfaced instead of silently passing the criterion.
- Keep the development service bound to localhost; authentication and deployment hardening are not implemented.
