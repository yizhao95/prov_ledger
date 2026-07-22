# provLedger convenience targets.

DEMO_DIR := examples/phantom-uplift
DEMO_VENV := $(DEMO_DIR)/.venv
PLUGIN_VENV := $(HOME)/skill-workspace/.venv

.PHONY: demo demo-clean

# One command, fresh clone -> the full phantom-uplift arc: false good news
# (green, pytest green, "+23.2% vs last week") -> contract MISMATCH
# (column_dropped: promo_discount) -> revise -> VERIFIED, real number +3.5%.
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
	       $(DEMO_DIR)/orders_fixed.json $(DEMO_DIR)/orders_drifted.json \
	       $(DEMO_DIR)/last_week_metrics.json $(DEMO_DIR)/declared_schema.json
