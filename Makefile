SHELL := /bin/bash
.SILENT:

.DEFAULT_GOAL := setup

GLOBAL_BIN_DIR ?= $(HOME)/bin
GLOBAL_BIN := $(GLOBAL_BIN_DIR)/curator
SHELL_RC ?= $(HOME)/.zshrc
VENV := .venv
PYTHON := $(VENV)/bin/python

.PHONY: setup venv install-global path test compile clean clean-venv uninstall-global help

setup: venv install-global path
	printf '\nCurator setup complete.\n'
	printf 'Open a new terminal, then run:\n'
	printf '  curator --help\n'

venv:
	source scripts/env >/dev/null

install-global: venv
	mkdir -p "$(GLOBAL_BIN_DIR)"
	printf '%s\n' '#!/usr/bin/env bash' >"$(GLOBAL_BIN)"
	printf '%s\n' 'set -euo pipefail' >>"$(GLOBAL_BIN)"
	printf '\n' >>"$(GLOBAL_BIN)"
	printf '%s\n' 'ROOT="$(CURDIR)"' >>"$(GLOBAL_BIN)"
	printf '%s\n' 'STAMP="$$ROOT/.venv/.curator-env-stamp"' >>"$(GLOBAL_BIN)"
	printf '\n' >>"$(GLOBAL_BIN)"
	printf '%s\n' 'if [[ ! -x "$$ROOT/.venv/bin/curator" || ! -f "$$STAMP" || "$$ROOT/pyproject.toml" -nt "$$STAMP" || "$$ROOT/scripts/env" -nt "$$STAMP" ]]; then' >>"$(GLOBAL_BIN)"
	printf '%s\n' '  (' >>"$(GLOBAL_BIN)"
	printf '%s\n' '    cd "$$ROOT"' >>"$(GLOBAL_BIN)"
	printf '%s\n' '    source scripts/env >/dev/null' >>"$(GLOBAL_BIN)"
	printf '%s\n' '  )' >>"$(GLOBAL_BIN)"
	printf '%s\n' 'fi' >>"$(GLOBAL_BIN)"
	printf '\n' >>"$(GLOBAL_BIN)"
	printf '%s\n' 'exec "$$ROOT/.venv/bin/curator" "$$@"' >>"$(GLOBAL_BIN)"
	chmod +x "$(GLOBAL_BIN)"
	printf 'Installed global wrapper: %s\n' "$(GLOBAL_BIN)"

path:
	mkdir -p "$(GLOBAL_BIN_DIR)"
	touch "$(SHELL_RC)"
	PATH_LINE='export PATH="$(GLOBAL_BIN_DIR):$$PATH"'; \
	if ! grep -qxF "$$PATH_LINE" "$(SHELL_RC)"; then \
	  printf '\n# Curator CLI\n%s\n' "$$PATH_LINE" >>"$(SHELL_RC)"; \
	  printf 'Added %s to PATH in %s\n' "$(GLOBAL_BIN_DIR)" "$(SHELL_RC)"; \
	else \
	  printf '%s already adds %s to PATH\n' "$(SHELL_RC)" "$(GLOBAL_BIN_DIR)"; \
	fi

test: venv
	"$(PYTHON)" -m unittest discover -s tests

compile: venv
	"$(PYTHON)" -m compileall -q src tests

clean:
	find src tests -type d -name '__pycache__' -prune -exec rm -rf {} +
	find src tests -type f -name '*.py[co]' -delete
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache *.egg-info

clean-venv:
	rm -rf "$(VENV)"

uninstall-global:
	rm -f "$(GLOBAL_BIN)"
	printf 'Removed global wrapper: %s\n' "$(GLOBAL_BIN)"

help:
	printf 'Curator make targets:\n'
	printf '  make              Set up venv, global wrapper, and shell PATH\n'
	printf '  make venv         Create/update .venv and editable install\n'
	printf '  make test         Run unit tests\n'
	printf '  make compile      Byte-compile src and tests\n'
	printf '  make clean        Remove Python/build caches\n'
	printf '  make clean-venv   Remove .venv\n'
	printf '  make uninstall-global  Remove ~/bin/curator wrapper\n'
