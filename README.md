# Section-wise Chunk Quality Judge

A standalone local FastAPI application that evaluates section-wise chunks against a PDFLens output package and creates an evidence-backed Excel report. It is separate from the PDFLens application.

The judge evaluates section-wise chunks against the DOCX supplied to the chunker. The score uses five LLM-judged criteria: C02 source accuracy, C06 condition/negation preservation, S01 section assignment, M01 topic coherence, and B03 chunk boundary quality. File parsing and source matching prepare evidence for LiteLLM; deterministic checks do not produce score outcomes. If LiteLLM is unavailable, the criteria are unresolved and no fallback score is calculated.

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
  "pass_candidate_at": 85,
  "review_required_below": 65
}
```

The pass threshold and review boundary control the decision label only; they do not change the LLM score formula.

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

Downloads are intentionally concise. The JSON report contains the decision, source/chunk summary, five-criterion scorecard, grouped next steps, and up to 40 evidence-backed FAIL/INCONCLUSIVE rows. The Excel workbook has four sheets: **Overview**, **Scorecard**, **Next Steps**, and **Evidence to Review**. Longer diagnostics remain in the live app; they are not duplicated into the default downloads.

The criteria version is **proposed-4.2-llm-points-sectionwise**. The score uses exactly five LLM-judged criteria: **C02 Source accuracy**, **C06 Conditions and negation**, **S01 Section assignment**, **M01 Topic coherence**, and **B03 Chunk boundaries**. The LLM compares each chunk with candidate passages and section context from the DOCX consumed by the chunker. Parsing and source matching only prepare evidence; deterministic checks and PDF-to-DOCX comparisons are diagnostics and do not affect the score. Each criterion result is worth **+1** for PASS, **−1** for FAIL, and **0** for INCONCLUSIVE or N/A. The rating is derived from those points: **((net points ÷ conclusive checks) + 1) ÷ 2 × 100**. Checks the LLM could not assess are excluded from the formula and shown separately. A score of 85% or higher meets the pass threshold; below 65% or a critical scored failure needs correction; 65–84% or unresolved criteria needs review. Thresholds are provisional. `judge_app/criteria.json` contains only these five scored criteria. The former broad catalogue is in `judge_app/diagnostic_criteria.json`; those checks can add diagnostic findings but never contribute to scoring or the quality gate.

## Optional on-prem LLM

No public model service is configured by default. LiteLLM Proxy is supported through its OpenAI-compatible Chat Completions API. Copy `.env.example` to `.env`, then edit the values for your proxy:

```sh
cp .env.example .env
```

Set `JUDGE_LLM_BASE_URL`, `JUDGE_LLM_MODEL`, and `JUDGE_LLM_API_KEY` in `.env`. The project loads this file at startup; shell environment variables take precedence. Keep `.env` private because it contains credentials, and restart the app after changing it. `JUDGE_LLM_BASE_URL` can be the proxy root (for example `http://127.0.0.1:4000`) or its `/v1` base URL; the app resolves either to the chat-completions route. `JUDGE_LLM_MODEL` must be the LiteLLM model alias available to that key. Set `JUDGE_LLM_JSON_MODE=false` if the selected LiteLLM model or deployment does not support JSON response mode. The evaluator sends compact chunk/source evidence, not the full PDF or full DOCX. The versioned LLM rubric is in `prompts/chunk_judge_v4.txt`; it tolerates coherent adjacent subsections in one chunk and requires evidence for failures. Reports record the prompt version. It includes five concise calibration examples for related versus unrelated section grouping, upstream omissions, insufficient evidence, and a provable condition reversal. Configure `JUDGE_LLM_ALLOW_REMOTE=true` only when the endpoint has been reviewed and approved as internal. The default host check permits loopback and private-network endpoints. LLM responses are schema-checked; malformed, unsupported, or ungrounded results become `INCONCLUSIVE` and require review.

## MVP boundaries

- Evaluation currently runs synchronously; large-document queueing, database persistence, object storage, authentication, and human-review writeback are later plan phases.
- Text alignment is deterministic and lexical. There is no embedding model yet; lexical match scores are candidate alignment evidence, not semantic proof.
- DOCX is the primary representation consumed by chunking. PDF text is used to flag possible upstream PDF-to-DOCX omissions.
- Some checks need page references, offsets, entity maps, or an approved local LLM. Missing evidence is surfaced instead of silently passing the criterion.
- Keep the development service bound to localhost; authentication and deployment hardening are not implemented.
