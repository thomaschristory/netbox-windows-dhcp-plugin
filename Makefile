COMPOSE = docker compose -f dev/docker-compose.yml
EXEC = $(COMPOSE) exec -T
PY = /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py

.PHONY: test test-netbox lint format dev down restart logs dist

test:
	uv run pytest -v

BASELINE = netbox_windows_dhcp_plugin/tests/query_counts.json
BRANCHING_BASELINE = tests_netbox/query_counts.branching.json

# The dev stack runs in branching mode, which adds queries to every page. Like CI, put the branching
# baselines in place for the run, copy them back (so UPDATE_QUERY_COUNTS=1 refreshes the branching file),
# then restore the standalone file.
test-netbox:
	@cp $(BASELINE) $(BASELINE).standalone && cp $(BRANCHING_BASELINE) $(BASELINE); \
	$(EXEC) -e DEBUG=false -e UPDATE_QUERY_COUNTS netbox $(PY) test --keepdb --noinput --parallel 1 \
		/plugin/tests_netbox; status=$$?; \
	cp $(BASELINE) $(BRANCHING_BASELINE); mv $(BASELINE).standalone $(BASELINE); exit $$status

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
