# Clea from a checkout. `make install` once, then `make run` (your accounts, in Clea's own window) or `make demo`
# (no accounts). People who only want to use it download Clea.app from the releases page instead.
PY ?= python3
BIN := .venv/bin
CLEA_HOME ?= $(HOME)/Library/Application Support/Clea/install
export CLEA_HOME

.PHONY: help install engine app window run serve demo test sim dmg clean
help:  ## what each target does
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-9s %s\n", $$1, $$2}'

install: engine app window  ## everything a first run needs: the engine, the app's screen and its window

engine:  ## the Python engine, in .venv
	$(PY) -m venv .venv
	$(BIN)/pip install -q -e ".[dev]"

app:  ## build the app's screen (ui/dist)
	cd ui && if command -v pnpm >/dev/null; then pnpm install --frozen-lockfile && pnpm build; else npm install && npm run build; fi

window:  ## the native window's dependencies (Electron)
	cd desktop && npm ci --no-audit --no-fund

run:  ## open Clea: its own window, which starts the engine and stops it when you quit
	cd desktop && CLEA_ENGINE="$(CURDIR)" npx electron .

serve:  ## the engine alone (worker and guard), with the app in a browser at http://127.0.0.1:4848
	$(BIN)/clea run

demo:  ## the app on a simulated workspace: no Slack, GitHub or model account needed
	env -u CLEA_HOME $(BIN)/python -m sim -k demo --serve

test:  ## the unit tests
	env -u CLEA_HOME $(BIN)/python -m pytest tests -q

sim:  ## every simulated scenario, on the scripted model
	env -u CLEA_HOME $(BIN)/python -m sim

dmg:  ## build the download: Clea.app with the engine and its own Python inside, and a disk image (desktop/out)
	zsh desktop/release.sh

clean:  ## remove what install built
	rm -rf .venv ui/dist ui/node_modules desktop/node_modules desktop/out *.egg-info
