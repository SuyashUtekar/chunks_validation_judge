# Section-wise Chunk Quality Judge

A standalone local FastAPI application that evaluates section-wise chunks against a PDFLens output package and creates an evidence-backed Excel report. It is separate from the PDFLens application.

The judge evaluates section-wise chunks against the DOCX supplied to the chunker. The judge asks five plain-language questions: does the chunk preserve the source meaning, sit under the right heading, stay on one subject, begin and end naturally, and include enough context? File parsing and source matching prepare evidence for LiteLLM; deterministic checks do not produce score outcomes. If LiteLLM is unavailable, the criteria are unresolved and no fallback score is calculated.

## Inputs

- A PDFLens ZIP containing the DOCX used for chunking, plus the original source PDF. The ZIP may also contain `metadata.json`, assets, and the source PDF. If both PDFs are supplied, their SHA-256 hashes must match. If a `manifest.json` exists, listed hashes are checked.
- The judge generates section-wise chunks from the DOCX inside the ZIP. A chunks JSON/XLSX/CSV upload is no longer required.
- Optional JSON configuration for evaluation thresholds and other judge options. The chunker creates one chunk for each detected section, keeps its body together, and does not apply a word target or repeat overlap text.

Example config:

```json
{
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

Open `http://127.0.0.1:8010/docs` for the upload form. Or submit the PDFLens ZIP and the original PDF; chunk generation happens inside the judge:

```sh
curl -f -X POST http://127.0.0.1:8010/api/v1/chunk-evaluations \
  -F 'pdflens_package=@/path/to/pdflens-output.zip' \
  -F 'source_pdf=@/path/to/original.pdf' \
  -F 'chunking_config={}'
```

The synchronous MVP responds with an evaluation ID and URLs for the JSON report, Excel workbook, and generated chunks JSON. Reports and generated chunks are stored under `data/evaluations/<evaluation_id>/`; uploaded PDF/DOCX files are not persisted. The default storage directory can be changed with `JUDGE_DATA_DIR`.

## Streamlit interface

The Streamlit interface takes the PDFLens output ZIP and original source PDF. It generates section-wise chunks from the DOCX in the ZIP, evaluates them, and offers the generated chunks JSON plus Excel and JSON reports for download. Start it from this folder:

```sh
uv run streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501
```

Or run `make run-ui`. Open `http://127.0.0.1:8501`. The optional configuration JSON is in the sidebar. The interface holds generated reports in the current Streamlit session for download; it does not save the uploaded files.

## Reports

Downloads are intentionally concise. The JSON report contains the decision, source/chunk summary, five-criterion scorecard, grouped next steps, and up to 40 evidence-backed FAIL/INCONCLUSIVE rows. The Excel workbook has four sheets: **Overview**, **Scorecard**, **Next Steps**, and **Evidence to Review**. Longer diagnostics remain in the live app; they are not duplicated into the default downloads.

The criteria version is **human-review-5.0-sectionwise**. The five scored questions are: **Meaning preserved**, **Right heading**, **One subject**, **Natural boundaries**, and **Enough context**. Every chunk receives a plain-language verdict, strengths supported by the source, problems with evidence, suggested improvements, and an action. Verdicts are **Looks good**, **Needs a fix**, **Check against source**, or **Could not evaluate**. Uncertain evidence is not treated as a failure. Detailed point scoring remains supporting information; reviewers should use the chunk-by-chunk evidence as the primary output. `judge_app/criteria.json` contains only these five scored criteria. The former broad catalogue is in `judge_app/diagnostic_criteria.json`; those checks can add diagnostic findings but never contribute to scoring or the quality gate.

## Optional on-prem LLM

No public model service is configured by default. LiteLLM Proxy is supported through its OpenAI-compatible Chat Completions API. Copy `.env.example` to `.env`, then edit the values for your proxy:

```sh
cp .env.example .env
```

Set `JUDGE_LLM_BASE_URL`, `JUDGE_LLM_MODEL`, and `JUDGE_LLM_API_KEY` in `.env`. The project loads this file at startup; shell environment variables take precedence. Keep `.env` private because it contains credentials, and restart the app after changing it. `JUDGE_LLM_BASE_URL` can be the proxy root (for example `http://127.0.0.1:4000`) or its `/v1` base URL; the app resolves either to the chat-completions route. `JUDGE_LLM_MODEL` must be the LiteLLM model alias available to that key. Set `JUDGE_LLM_JSON_MODE=false` if the selected LiteLLM model or deployment does not support JSON response mode. Set `JUDGE_LLM_MAX_WORKERS` to control simultaneous chunk evaluations (default 4, maximum 8); lower it if your on-prem endpoint returns rate-limit or overload errors. The evaluator sends compact chunk/source evidence, not the full PDF or full DOCX. The versioned LLM rubric is in `prompts/chunk_judge_v5.txt`; it uses the five reviewer questions, requires evidence for failures, and supplies a practical improvement/action for each confirmed problem. Reports record the prompt version. Configure `JUDGE_LLM_ALLOW_REMOTE=true` only when the endpoint has been reviewed and approved as internal. The default host check permits loopback and private-network endpoints. LLM responses are schema-checked; malformed, unsupported, or ungrounded results become `INCONCLUSIVE` and require review.

## MVP boundaries

- Evaluation currently runs synchronously; large-document queueing, database persistence, object storage, authentication, and human-review writeback are later plan phases.
- Text alignment is deterministic and lexical. There is no embedding model yet; lexical match scores are candidate alignment evidence, not semantic proof.
- DOCX is the primary representation consumed by chunking. PDF text is used to flag possible upstream PDF-to-DOCX omissions.
- Some checks need page references, offsets, entity maps, or an approved local LLM. Missing evidence is surfaced instead of silently passing the criterion.
- Keep the development service bound to localhost; authentication and deployment hardening are not implemented.
