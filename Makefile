# Clea from a checkout. `make install` once, then `make run` (your accounts) or `make demo` (no accounts).
PY ?= python3
BIN := .venv/bin
CLEA_HOME ?= $(HOME)/Library/Application Support/Clea/install
export CLEA_HOME

.PHONY: help install engine app run demo test sim desktop clean
help:  ## what each target does
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-9s %s\n", $$1, $$2}'

install: engine app  ## everything a first run needs: the engine and the app's screen

engine:  ## the Python engine, in .venv
	$(PY) -m venv .venv
	$(BIN)/pip install -q -e ".[dev]"

app:  ## build the app's screen (ui/dist)
	cd ui && if command -v pnpm >/dev/null; then pnpm install --frozen-lockfile && pnpm build; else npm install && npm run build; fi

run:  ## start Clea on this Mac: the worker, the guard and the app on http://127.0.0.1:4848
	$(BIN)/clea run

demo:  ## the app on a simulated workspace: no Slack, GitHub or model account needed
	env -u CLEA_HOME $(BIN)/python -m sim -k demo --serve

test:  ## the unit tests
	env -u CLEA_HOME $(BIN)/python -m pytest tests -q

sim:  ## every simulated scenario, on the scripted model
	env -u CLEA_HOME $(BIN)/python -m sim

desktop:  ## the native window, running this checkout's engine
	cd desktop && npm install && CLEA_ENGINE="$(CURDIR)" npm start

clean:  ## remove what install built
	rm -rf .venv ui/dist ui/node_modules desktop/node_modules desktop/out *.egg-info
