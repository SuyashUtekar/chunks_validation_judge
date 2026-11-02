.PHONY: install run run-ui

install:
	uv sync

run:
	uv run uvicorn judge_app.main:app --reload --host 127.0.0.1 --port 8010

run-ui:
	uv run streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501
