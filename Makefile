# Use the Compose builder directly; override with COMPOSE_BAKE=true if needed.
export COMPOSE_BAKE ?= false

COMPOSE = docker compose -f compose.yaml -f compose.dev.yaml
.PHONY: up down logs test shell
up:
	$(COMPOSE) up -d --build
down:
	$(COMPOSE) down
logs:
	$(COMPOSE) logs -f core
test:
	$(COMPOSE) run --rm --build core python -m pytest -q -p no:cacheprovider
shell:
	$(COMPOSE) exec core sh
