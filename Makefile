COMPOSE = docker compose -f dev/docker-compose.yml
EXEC = $(COMPOSE) exec -T
PY = /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py

.PHONY: test test-netbox lint format dev down restart logs dist

test:
	uv run pytest -v

test-netbox:
	$(EXEC) -e DEBUG=false netbox $(PY) test --keepdb --noinput --parallel 1 /plugin/tests_netbox

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

dev:
	$(COMPOSE) up -d --build --wait --wait-timeout 1200

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart netbox netbox-worker

logs:
	$(COMPOSE) logs -f netbox

dist:
	rm -rf dist
	uv build
	uvx twine check --strict dist/*
	python3 dev/scripts/check_dist.py dist
