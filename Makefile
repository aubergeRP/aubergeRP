export UID := $(shell id -u)
export GID := $(shell id -g)

# ─── Paths ────────────────────────────────────────────────────────────────────
DOCKER_DIR       := docker
PROFILES_DIR     := $(DOCKER_DIR)/profiles
COMPOSE_BASE     := $(DOCKER_DIR)/docker-compose.yml
COMPOSE_LOCALAI  := $(DOCKER_DIR)/docker-compose.localai.yml

# ─── Profile detection ────────────────────────────────────────────────────────
AVAILABLE_PROFILES := $(patsubst $(PROFILES_DIR)/%.yml,%,$(wildcard $(PROFILES_DIR)/*.yml))
LEGACY_GPU := $(word 2,$(MAKECMDGOALS))
GPU ?= $(gpu)

compose_args = -f $(COMPOSE_BASE)$(if $(strip $(GPU)), -f $(COMPOSE_LOCALAI) -f $(PROFILES_DIR)/$(GPU).yml)

# Read a value from the x-models section of a profile YAML
# Usage: $(call profile_get,key,profile)
profile_get = $(shell sed -n 's/^[[:space:]]*$(1)[[:space:]]*:[[:space:]]*//p' $(PROFILES_DIR)/$(2).yml | sed 's/^&[^ ]* //; s/[[:space:]]*#.*//')

# ─── Terminal colours ─────────────────────────────────────────────────────────
GREEN  := \033[1;32m
YELLOW := \033[1;33m
BLUE   := \033[1;34m
RED    := \033[1;31m
RESET  := \033[0m

.PHONY: run test test-cov test-e2e lint lint-fix doc tag help \
        docker stop clean logs \
	_compose-up _localai-install \
	$(AVAILABLE_PROFILES)

# ─── Help ─────────────────────────────────────────────────────────────────────
help:
	@printf "$(BLUE)aubergeRP$(RESET) — available commands\n\n"
	@printf "  $(YELLOW)Development$(RESET)\n"
	@printf "    make run              Start dev server (hot-reload, port 8123)\n"
	@printf "    make test                        Run Python test suite\n"
	@printf "    make test tests/test_api.py      Run one file\n"
	@printf "    make test-cov         Run full suite with coverage + JUnit reports\n"
	@printf "    make test-e2e         Run browser e2e tests (requires node + playwright)\n"
	@printf "    make lint             Run ruff + mypy\n"
	@printf "    make lint-fix         Fix linting issues automatically (ruff --fix)\n"
	@printf "    make doc              Regenerate docs/03-backend-api.md from source\n"
	@printf "\n"
	@printf "  $(YELLOW)Release$(RESET)\n"
	@printf "    make tag v=1.2.3      Bump pyproject, commit, tag v1.2.3 and push\n"
	@printf "                          (triggers the GHCR image build + GitHub release)\n"
	@printf "\n"
	@printf "  $(YELLOW)Docker stack$(RESET)\n"
	@printf "    make docker           Start auberge-app only\n"
	@printf "    make docker gpu=<profile>\n"
	@printf "                          Start LocalAI + auberge-app, install models via gallery\n"
	@printf "    make stop [gpu=...]   Stop running containers\n"
	@printf "    make clean [gpu=...]  Stop and remove containers and networks\n"
	@printf "    make logs [gpu=...]   Tail container logs\n"
	@printf "\n"
	@printf "  $(YELLOW)Available profiles$(RESET)\n"
	@for p in $(AVAILABLE_PROFILES); do \
		printf "    make docker gpu=$$p\n"; \
	done
	@printf "\n"
	@printf "  $(YELLOW)Examples$(RESET)\n"
	@printf "    make docker\n"
	@printf "    make docker gpu=rtx3090\n"
	@printf "    make stop gpu=rtx3090\n"
	@printf "    make clean gpu=rtx3090\n"

# ─── Dev targets ──────────────────────────────────────────────────────────────
run:
	python -m uvicorn aubergeRP.main:app --reload --host 0.0.0.0 --port 8123

_TEST_ARGS := $(filter-out test, $(MAKECMDGOALS))
test:
	python -m pytest $(or $(_TEST_ARGS),tests/)

# Full suite with coverage + machine-readable reports (used by CI)
test-cov:
	python -m pytest tests/ \
		--junitxml=reports/junit.xml \
		--cov=aubergeRP --cov-report=xml:reports/coverage.xml --cov-report=term-missing

test-e2e:
	cd tests/e2e && node --test chat-streaming.test.mjs generate-image-button.test.mjs mobile-scroll.test.mjs markdown-rendering.test.mjs

lint:
	python -m ruff check aubergeRP/ tests/
	python -m mypy aubergeRP/

lint-fix:
	python -m ruff check aubergeRP/ tests/ --fix --unsafe-fixes

doc:
	python scripts/generate_api_docs.py

# ─── Release: make tag v=1.2.3 ───────────────────────────────────────────────
# Bumps the version in pyproject.toml, commits, tags v<version> and pushes.
# Pushing the tag triggers .github/workflows/docker-publish.yml, which builds
# the GHCR image and opens the matching GitHub release.
#
# Naming convention: the *git tag* carries the `v` prefix (v1.2.3) because the
# workflow triggers on `v*`; pyproject.toml and the *Docker tag* do not (1.2.3).
# `v=1.2.3` and `v=v1.2.3` are both accepted — the prefix is normalised away.
RELEASE_VERSION := $(patsubst v%,%,$(strip $(v)))

tag:
	@if [ -z "$(RELEASE_VERSION)" ]; then \
		printf "$(RED)Error:$(RESET) missing version. Use: make tag v=1.2.3\n"; exit 1; \
	fi
	@echo "$(RELEASE_VERSION)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$$' || { \
		printf "$(RED)Error:$(RESET) '$(v)' is not a valid semver (e.g. 1.2.3 or v1.2.3).\n"; exit 1; }
	@[ -z "$$(git status --porcelain)" ] || { \
		printf "$(RED)Error:$(RESET) working tree is not clean — commit or stash first.\n"; exit 1; }
	@[ "$$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { \
		printf "$(RED)Error:$(RESET) releases are tagged from main only.\n"; exit 1; }
	@git rev-parse -q --verify "refs/tags/v$(RELEASE_VERSION)" >/dev/null && { \
		printf "$(RED)Error:$(RESET) tag v$(RELEASE_VERSION) already exists.\n"; exit 1; } || true
	@printf "  $(BLUE)→$(RESET) Bumping pyproject.toml to $(RELEASE_VERSION)...\n"
	@sed -i '0,/^version = ".*"$$/s//version = "$(RELEASE_VERSION)"/' pyproject.toml
	@grep -q '^version = "$(RELEASE_VERSION)"$$' pyproject.toml || { \
		printf "$(RED)Error:$(RESET) failed to update version in pyproject.toml.\n"; \
		git checkout -- pyproject.toml; exit 1; }
	@git add pyproject.toml
	@git commit -m "chore(release): v$(RELEASE_VERSION)"
	@git tag -a "v$(RELEASE_VERSION)" -m "v$(RELEASE_VERSION)"
	@printf "  $(BLUE)→$(RESET) Pushing main and tag v$(RELEASE_VERSION)...\n"
	@git push origin main
	@git push origin "v$(RELEASE_VERSION)"
	@printf "  $(GREEN)✓$(RESET) v$(RELEASE_VERSION) pushed — watch the build:\n"
	@printf "    https://github.com/aubergeRP/aubergeRP/actions\n"

# ─── Docker: make docker [gpu=<profile>] ─────────────────────────────────────
docker:
	@if [ -n "$(LEGACY_GPU)" ]; then \
		printf "$(RED)Error:$(RESET) Positional profiles are no longer supported.\n"; \
		printf "       Use: make docker gpu=$(LEGACY_GPU)\n"; \
		exit 1; \
	fi
	@if [ -n "$(GPU)" ] && [ ! -f "$(PROFILES_DIR)/$(GPU).yml" ]; then \
		printf "$(RED)Error:$(RESET) GPU profile '$(GPU)' does not exist.\n"; \
		printf "Profiles: $(AVAILABLE_PROFILES)\n"; \
		exit 1; \
	fi
	@if [ -n "$(GPU)" ]; then \
		$(MAKE) --no-print-directory _setup-$(GPU) GPU=$(GPU); \
	else \
		$(MAKE) --no-print-directory _compose-up; \
	fi
stop:
	@printf "  $(YELLOW)→$(RESET) Stopping stack...\n"
	@docker compose $(call compose_args) stop

clean:
	@printf "  $(RED)→$(RESET) Removing containers and networks...\n"
	@docker compose $(call compose_args) down

logs:
	@docker compose $(call compose_args) logs -f

# Explicitly reject legacy positional profile usage.
$(AVAILABLE_PROFILES):
	@printf "$(RED)Error:$(RESET) Positional profiles are no longer supported.\n"
	@printf "       Use: make docker gpu=$@\n"
	@exit 1

# ─── Internal: compose up ─────────────────────────────────────────────────────
_compose-up:
	@if [ -n "$(GPU)" ]; then \
		printf "  $(BLUE)→$(RESET) Starting GPU stack [$(GPU)]...\n"; \
	else \
		printf "  $(BLUE)→$(RESET) Starting app-only stack...\n"; \
	fi
	@docker compose $(call compose_args) pull
	@docker compose $(call compose_args) \
		up -d --remove-orphans --build

# ─── Internal: install a model via LocalAI gallery API ───────────────────────
# Usage: $(MAKE) _localai-install NAME=model-name
# The install is asynchronous: LocalAI downloads the model in the background.
_localai-install:
	@printf "  $(BLUE)→$(RESET) Waiting for LocalAI API...\n"
	@until curl -sf http://localhost:8080/v1/models >/dev/null 2>&1; do sleep 2; done
	@if curl -sf http://localhost:8080/v1/models 2>/dev/null | grep -q '"$(NAME)"'; then \
		printf "  $(GREEN)✓$(RESET) $(NAME) already installed.\n"; \
	else \
		printf "  $(BLUE)↓$(RESET) Installing $(NAME) (downloading in background)...\n"; \
		curl -sf -X POST "http://localhost:8080/api/models/install/$(NAME)" \
			-H "Content-Type: application/json" -d '{}' >/dev/null \
			&& printf "  $(GREEN)✓$(RESET) $(NAME) install started — check 'make logs' for progress.\n" \
			|| printf "  $(RED)✗$(RESET) Failed to request install for $(NAME).\n"; \
	fi

# ─── Generic profile setup ────────────────────────────────────────────────────
_setup-%:
	@$(MAKE) --no-print-directory _compose-up GPU=$*
	@$(MAKE) --no-print-directory _localai-install \
		NAME=$(call profile_get,llm_name,$*)
	@$(MAKE) --no-print-directory _localai-install \
		NAME=$(call profile_get,img_name,$*)

# Catch-all: silently absorb extra arguments passed to `make test <file>`
%:
	@:
