# Courtroom IndicXlit runtime — shippable entry points.
# Run every target from this directory.

PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
VERSION ?= hi-1.0.0
CENTRAL_PORT ?= 8700
LOCAL_PORT ?= 8765
export PYTHONPATH := $(CURDIR)

.PHONY: help setup setup-runtime warm-model publish central local test wan-proxy \
        bench-cost bench-http-selftest bench-latency bench-quality bench-fleet logs clean demo-check

help: ## show targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-22s %s\n", $$1, $$2}'

setup-runtime: ## venv + minimal deps (tests, cost model, HTTP self-test; no torch)
	python3.10 -m venv .venv
	$(PIP) install -U 'pip<24.1'
	$(PIP) install -r requirements-runtime.txt

setup: setup-runtime ## full ML stack (IndicXlit + torch). Needs pip<24.1 + --no-deps.
	$(PIP) install --no-deps -r requirements-ml.txt

warm-model: ## download stock IndicXlit weights + Hindi rescore dict once
	$(PY) -c "from ai4bharat.transliteration import XlitEngine; XlitEngine('hi', rescore=True)"

publish: ## publish library weights into var/registry as $(VERSION)
	$(PY) -m xlit.central.publish --version $(VERSION)

central: ## Central Model Registry on :$(CENTRAL_PORT)
	$(PY) -m xlit.central.server --port $(CENTRAL_PORT)

local: ## Local Xlit Service + editor UI on :$(LOCAL_PORT)
	$(PY) -m xlit.local.server --central-url http://127.0.0.1:$(CENTRAL_PORT) --port $(LOCAL_PORT)

test: ## unit + e2e tests (fake engine; no model download)
	$(PY) -m unittest discover -s tests -t . -v

bench-cost: ## print + write results/cost_model.json
	$(PY) -m benchmarks.cost_model --json results/cost_model.json

bench-http-selftest: ## HTTP typing scenarios against in-process fake stack
	$(PY) -m benchmarks.latency_http --self-test --scenario all --out results/latency_http_selftest.json

bench-latency: ## stock-engine compute sweep (needs ML setup + warm-model)
	$(PY) benchmarks/latency_baseline.py --out results/latency_baseline.json

bench-quality: ## quality gate on checked-in sample (stock arm; needs ML setup)
	$(PY) -m benchmarks.quality_dakshina --arms stock --out results/quality_stock.json

wan-proxy: ## court-link emulator on :8701 in front of central (60 ms RTT, 100 Mbps shared)
	$(PY) -m benchmarks.wan_proxy --listen 127.0.0.1:8701 --upstream 127.0.0.1:$(CENTRAL_PORT) \
		--rtt-ms 60 --jitter-ms 10 --down-mbps 100 --up-mbps 20 --drop-per-mb 0.002

bench-fleet: ## 16 fresh sessions through wan-proxy + 16 typists (needs central + wan-proxy running)
	$(PY) -m benchmarks.fleet_stress --sessions 16 --fresh --central-url http://127.0.0.1:8701 \
		--out results/stress_fleet_cold_wan.json

demo-check: test bench-cost bench-http-selftest ## everything that runs without the model
	@echo "demo-check OK"

logs: ## follow JSON logs
	tail -F logs/central_server.log logs/local_client.log

clean: ## wipe generated registry / store / logs (keeps results/ and .venv)
	rm -rf var logs
