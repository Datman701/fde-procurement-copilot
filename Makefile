PYTHON ?= .venv/bin/python

.PHONY: run run-streamlit ui-install ui-build ui-dev test eval verify

run:
	$(PYTHON) run_local.py

run-streamlit:
	$(PYTHON) -m streamlit run app/ui.py --server.port 8501 --server.headless true

ui-install:
	cd frontend && npm install

ui-build: ui-install
	cd frontend && npm run build

ui-dev: ui-install
	cd frontend && npm run dev

test:
	$(PYTHON) -m pytest tests/ -q

eval:
	$(PYTHON) -m eval.run --arch both --split all

verify:
	$(PYTHON) verify_setup.py
