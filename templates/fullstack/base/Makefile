SHELL := /bin/bash
.DEFAULT_GOAL := dev
.PHONY: dev check smoke infra-down

BACKEND_DIR ?= $(patsubst %/,%,$(dir $(firstword $(wildcard */module.toml))))
FRONTEND_DIR ?= $(patsubst %/,%,$(dir $(firstword $(wildcard */package.json))))

ifeq ($(strip $(BACKEND_DIR)),)
$(error Cannot find backend directory: expected one child directory containing module.toml)
endif
ifeq ($(strip $(FRONTEND_DIR)),)
$(error Cannot find frontend directory: expected one child directory containing package.json)
endif

dev:
	@set -e; \
	$(MAKE) -C "$(BACKEND_DIR)" dev & backend_pid=$$!; \
	(cd "$(FRONTEND_DIR)" && pnpm dev) & frontend_pid=$$!; \
	kill_tree() { \
		for child in $$(pgrep -P "$$1" 2>/dev/null || true); do kill_tree "$$child"; done; \
		kill -INT "$$1" 2>/dev/null || true; \
	}; \
	stop() { kill_tree "$$backend_pid"; kill_tree "$$frontend_pid"; }; \
	trap stop INT TERM EXIT; \
	while kill -0 $$backend_pid 2>/dev/null && kill -0 $$frontend_pid 2>/dev/null; do sleep 1; done; \
	stop; \
	wait $$backend_pid 2>/dev/null || backend_status=$$?; \
	wait $$frontend_pid 2>/dev/null || frontend_status=$$?; \
	test "$${backend_status:-0}" -eq 0 -a "$${frontend_status:-0}" -eq 0

check:
	$(MAKE) -C "$(BACKEND_DIR)" check
	cd "$(FRONTEND_DIR)" && pnpm lint && pnpm test && pnpm build

smoke:
	@curl --fail --silent -H 'Origin: http://localhost:3000' http://localhost:8000/health/live >/dev/null
	@curl --fail --silent http://localhost:3000 >/dev/null
	@echo "fullstack smoke: ok"

infra-down:
	$(MAKE) -C "$(BACKEND_DIR)" infra-down
