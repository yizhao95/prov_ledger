# provLedger convenience targets.

DEMO_DIR := examples/silent-class-drop
DEMO_VENV := $(DEMO_DIR)/.venv
PLUGIN_VENV := $(HOME)/skill-workspace/.venv

.PHONY: demo demo-clean

# One command, fresh clone -> the full silent-class-drop arc:
# v1 (green but MISMATCH, purity 0.31) -> revise -> v2 (VERIFIED, purity 0.91).
# Reuses the plugin's unified venv (created by scripts/bootstrap.sh) when it
# exists; otherwise creates a local one (needs the python3-venv package).
demo:
	@if [ -x "$(PLUGIN_VENV)/bin/python" ]; then \
	    PY="$(PLUGIN_VENV)/bin/python"; \
	else \
	    python3 -m venv $(DEMO_VENV) || { \
	        echo "could not create a venv — install python3-venv, or run"; \
	        echo "  <your-python> -m pip install -r $(DEMO_DIR)/requirements.txt"; \
	        echo "  <your-python> $(DEMO_DIR)/run_demo.py"; exit 1; }; \
	    PY="$(DEMO_VENV)/bin/python"; \
	fi; \
	$$PY -m pip --version >/dev/null 2>&1 || $$PY -m ensurepip --upgrade >/dev/null; \
	$$PY -m pip install -q -r $(DEMO_DIR)/requirements.txt && \
	$$PY $(DEMO_DIR)/run_demo.py

demo-clean:
	rm -rf $(DEMO_VENV) $(DEMO_DIR)/demo-orchestrator.db \
	       $(DEMO_DIR)/upstream_fixed.json $(DEMO_DIR)/upstream_drifted.json \
	       $(DEMO_DIR)/ground_truth.csv
