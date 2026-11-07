# wagrep: no build step. These targets install the launcher and run checks.
PREFIX ?= $(HOME)/.local
BIN := $(PREFIX)/bin
PYTHON ?= python3

.PHONY: install uninstall test check

install:            ## symlink ./wagrep into $(BIN) (default ~/.local/bin)
	@mkdir -p "$(BIN)"
	@ln -sfn "$(CURDIR)/wagrep" "$(BIN)/wagrep"
	@echo "installed $(BIN)/wagrep -> $(CURDIR)/wagrep"
	@echo "first run builds the index: wagrep index"

uninstall:          ## remove the symlink (the index stays in ~/.cache/wagrep)
	@rm -f "$(BIN)/wagrep"
	@echo "removed $(BIN)/wagrep; delete ~/.cache/wagrep to drop the index"

test:               ## unit tests, warnings as errors
	@$(PYTHON) -W error::ResourceWarning -m unittest -q

check: test         ## tests plus a byte-compile of every module
	@$(PYTHON) -m compileall -q src tests wagrep >/dev/null && echo "compile ok"
