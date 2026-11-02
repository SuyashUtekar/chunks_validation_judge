# Section-wise Chunk Quality Judge

A standalone local FastAPI application that evaluates section-wise chunks against a PDFLens output package and creates an evidence-backed Excel report. It is separate from the PDFLens application.

This is the first deterministic MVP from the implementation plan. It performs artifact validation, DOCX/PDF evidence extraction, section-aware chunk normalization, source alignment, rule-based checks, scoring, and Excel/JSON reporting. Contextual criteria are evaluated through an optional OpenAI-compatible **on-prem** model endpoint. Without that endpoint, those criteria are recorded as `INCONCLUSIVE`, never as `PASS`.

## Inputs

- A PDFLens ZIP containing exactly one source PDF and optionally `document.docx` and `metadata.json`. If a `manifest.json` exists, listed hashes are checked.
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
  "source_unit_coverage_pass_threshold": 95
}
```

No page/section/entity fields are assumed mandatory when the chunk source does not provide them; their findings become `INCONCLUSIVE` or `N/A` as appropriate.

## Run locally

From this folder:

```sh
uv sync
uv run uvicorn judge_app.main:app --reload --host 127.0.0.1 --port 8010
```

Open `http://127.0.0.1:8010/docs` for the upload form. Or submit the PDFLens ZIP and generated chunks:

```sh
curl -f -X POST http://127.0.0.1:8010/api/v1/chunk-evaluations \
  -F 'pdflens_package=@/path/to/pdflens-output.zip' \
  -F 'chunks_file=@/path/to/chunks.xlsx' \
  -F 'chunking_config={"strategy":"sectionwise","max_words":800,"overlap_words":80}'
```

The synchronous MVP responds with an evaluation ID and URLs for the JSON report and Excel workbook. Reports are stored under `data/evaluations/<evaluation_id>/`; input PDF/DOCX/chunk files are not persisted. The default storage directory can be changed with `JUDGE_DATA_DIR`.

## Streamlit interface

The Streamlit interface takes the same PDFLens ZIP and a chunks JSON file, displays the summary and findings, and provides downloadable Excel and JSON reports. Start it from this folder:

```sh
uv run streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501
```

Or run `make run-ui`. Open `http://127.0.0.1:8501`. The optional configuration JSON is in the sidebar. The interface holds generated reports in the current Streamlit session for download; it does not save the uploaded files.

## Reports

The workbook has these sheets:

- **Summary** — quality gate, pooled scores, coverage, upstream discrepancy counts, and model state.
- **Chunk Results** — per-chunk alignment, criteria counts, score, and gate.
- **Findings** — criterion outcome, severity, explanation, evidence IDs, short evidence excerpts, and review flag.
- **Sections** — section coverage and aggregate scores.
- **Coverage** — source units missing from chunks and PDF-to-DOCX upstream discrepancies.
- **Criteria** — the versioned baseline criteria and their aggregate outcomes.
- **Input Audit** — input hashes, filenames, config, and extraction/model provenance.

The initial criteria version is marked **proposed**. Suggested 95%/85% score bands are provisional plan values, not calibrated release thresholds. A critical failure overrides the score. `PASS` and `FAIL` alone contribute to percentages; `N/A` and `INCONCLUSIVE` are reported separately.

## Optional on-prem LLM

No public model service is configured by default. LiteLLM Proxy is supported through its OpenAI-compatible Chat Completions API. Configure the proxy base URL and a model alias enabled in LiteLLM before starting the app:

```sh
export JUDGE_LLM_BASE_URL='http://127.0.0.1:4000/v1'
export JUDGE_LLM_MODEL='your-litellm-model-alias'
export JUDGE_LLM_API_KEY='your-litellm-virtual-key'
```

`JUDGE_LLM_BASE_URL` can be the proxy root (for example `http://127.0.0.1:4000`) or its `/v1` base URL; the app resolves either to the chat-completions route. `JUDGE_LLM_MODEL` must be the LiteLLM model alias available to that key. Set `JUDGE_LLM_JSON_MODE=false` if the selected LiteLLM model or deployment does not support JSON response mode. The evaluator sends compact chunk/source evidence, not the full PDF or full DOCX. The strict, versioned rubric is in `prompts/chunk_judge_v2.txt`; reports record the prompt version. Configure `JUDGE_LLM_ALLOW_REMOTE=true` only when the endpoint has been reviewed and approved as internal. The default host check permits loopback and private-network endpoints. LLM responses are schema-checked; malformed, unsupported, or ungrounded results become `INCONCLUSIVE` and require review.

## MVP boundaries

- Evaluation currently runs synchronously; large-document queueing, database persistence, object storage, authentication, and human-review writeback are later plan phases.
- Text alignment is deterministic and lexical. There is no embedding model yet; lexical match scores are candidate alignment evidence, not semantic proof.
- DOCX is the primary representation consumed by chunking. PDF text is used to flag possible upstream PDF-to-DOCX omissions.
- Some checks need page references, offsets, entity maps, or an approved local LLM. Missing evidence is surfaced instead of silently passing the criterion.
- Keep the development service bound to localhost; authentication and deployment hardening are not implemented.
