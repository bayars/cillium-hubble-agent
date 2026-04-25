# Network Monitor - Makefile
#
# Usage:
#   make help          Show available targets
#   make test          Run unit tests
#   make lint          Run linter
#   make build         Build all Docker images
#   make deploy        Deploy to Kubernetes via Helm

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

REGISTRY        ?= ghcr.io/bayars
SIDECAR_IMAGE   ?= $(REGISTRY)/netmon-sidecar
COLLECTOR_IMAGE ?= $(REGISTRY)/netmon-collector
HUBBLE_IMAGE    ?= $(REGISTRY)/netmon-hubble-collector
TAG             ?= latest
NAMESPACE      ?= network-monitor
HELM_RELEASE   ?= network-monitor
HELM_CHART     ?= helm/network-monitor

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------

.PHONY: install
install: ## Install dependencies
	uv sync --dev

.PHONY: dev
dev: ## Run API server in development mode
	uv run uvicorn api.main:app --reload

.PHONY: lint
lint: ## Run ruff linter
	uv run ruff check sidecar/ hubble_collector/ tests/ --exclude api/generated/

.PHONY: lint-fix
lint-fix: ## Run ruff linter with auto-fix
	uv run ruff check sidecar/ hubble_collector/ tests/ --exclude api/generated/ --fix

.PHONY: test
test: ## Run unit tests
	uv run pytest tests/ -v --tb=short

.PHONY: test-ci
test-ci: ## Run tests with JUnit XML output for CI
	uv run pytest tests/ -v --tb=short --junitxml=report.xml

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

.PHONY: build
build: build-sidecar build-collector build-hubble ## Build all Docker images

.PHONY: build-sidecar
build-sidecar: ## Build sidecar agent Docker image
	docker build -t $(SIDECAR_IMAGE):$(TAG) -f sidecar/Dockerfile .

.PHONY: build-collector
build-collector: ## Build standalone collector Docker image
	docker build -t $(COLLECTOR_IMAGE):$(TAG) -f sidecar/Dockerfile.collector .

.PHONY: build-hubble
build-hubble: ## Build Hubble flow collector Docker image
	docker build -t $(HUBBLE_IMAGE):$(TAG) -f hubble_collector/Dockerfile .

.PHONY: push
push: push-sidecar push-collector push-hubble ## Push all Docker images

.PHONY: push-sidecar
push-sidecar: ## Push sidecar agent Docker image
	docker push $(SIDECAR_IMAGE):$(TAG)

.PHONY: push-collector
push-collector: ## Push standalone collector Docker image
	docker push $(COLLECTOR_IMAGE):$(TAG)

.PHONY: push-hubble
push-hubble: ## Push Hubble flow collector Docker image
	docker push $(HUBBLE_IMAGE):$(TAG)

# ---------------------------------------------------------------------------
# Docker Compose
# ---------------------------------------------------------------------------

.PHONY: up
up: ## Start services via docker-compose
	docker-compose up -d

.PHONY: down
down: ## Stop services via docker-compose
	docker-compose down

.PHONY: logs
logs: ## Tail docker-compose logs
	docker-compose logs -f

# ---------------------------------------------------------------------------
# Helm / Kubernetes
# ---------------------------------------------------------------------------

.PHONY: helm-lint
helm-lint: ## Lint Helm chart
	helm lint $(HELM_CHART)

.PHONY: helm-template
helm-template: ## Render Helm chart templates
	helm template $(HELM_RELEASE) $(HELM_CHART) --namespace $(NAMESPACE)

.PHONY: helm-template-collector
helm-template-collector: ## Render Helm chart with collector enabled
	helm template $(HELM_RELEASE) $(HELM_CHART) --namespace $(NAMESPACE) \
		--set collector.enabled=true \
		--set collector.pollIntervalMs=2000

.PHONY: deploy
deploy: ## Deploy to Kubernetes via Helm
	helm upgrade --install $(HELM_RELEASE) $(HELM_CHART) \
		--namespace $(NAMESPACE) --create-namespace

.PHONY: deploy-collector
deploy-collector: ## Deploy with collector enabled
	helm upgrade --install $(HELM_RELEASE) $(HELM_CHART) \
		--namespace $(NAMESPACE) --create-namespace \
		--set collector.enabled=true \
		--set collector.pollIntervalMs=2000

.PHONY: undeploy
undeploy: ## Remove Helm release
	helm uninstall $(HELM_RELEASE) --namespace $(NAMESPACE)

# ---------------------------------------------------------------------------
# CI checks (runs lint + test + helm-lint together)
# ---------------------------------------------------------------------------

.PHONY: ci
ci: lint test helm-lint ## Run lint + test + helm-lint

# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------

.PHONY: clean
clean: ## Remove build artifacts
	rm -rf report.xml .pytest_cache __pycache__ .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
