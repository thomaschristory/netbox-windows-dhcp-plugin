COMPOSE = docker compose -f dev/docker-compose.yml
EXEC = $(COMPOSE) exec -T
PY = /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py

.PHONY: test test-lab lab-e2e test-netbox lint format dev down restart logs dist docs docs-serve

test:
	uv run pytest -v

# Opt-in: the real client against the lab Windows DHCP server named in dev/env/lab.env (see tests/lab/conftest.py).
# Creates and removes nbwd-test-* objects in 198.18.0.0/15 and 2001:db8:4e42::/64 only.
test-lab:
	WINDOWS_DHCP_LAB=1 uv run pytest -v -p no:cacheprovider tests/lab

# Opt-in: Windows -> NetBox -> Windows on the dev stack against the lab server (see dev/scripts/lab_e2e.py).
lab-e2e:
	$(EXEC) netbox $(PY) shell -c "exec(open('/plugin/dev/scripts/lab_e2e.py').read())"

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

# Zensical publishes every file under docs_dir and has no exclude setting, so the site is built from a staging copy
# of the files git can see (tracked, or untracked but not ignored). Locally ignored files never reach site/.
DOCS_STAGE = build/docs-src

docs:
	rm -rf $(DOCS_STAGE) site
	mkdir -p $(DOCS_STAGE)
	git ls-files -z --cached --others --exclude-standard -- docs mkdocs.yml CHANGELOG.md \
		| tar --null -T - -cf - | tar -xf - -C $(DOCS_STAGE)
	cd $(DOCS_STAGE) && uv run --locked --project $(CURDIR) --group docs zensical build --strict
	mv $(DOCS_STAGE)/site site

# Serves the working tree directly, for local preview only. Port 8001: the dev stack uses 8000.
docs-serve:
	uv run --group docs zensical serve -a localhost:8001
