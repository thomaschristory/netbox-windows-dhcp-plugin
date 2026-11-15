COMPOSE = docker compose -f dev/docker-compose.yml
EXEC = $(COMPOSE) exec -T
PY = /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py

.PHONY: test test-lab lab-e2e lab-seed lab-wipe test-netbox lint format dev down restart logs dist docs docs-serve

test:
	uv run pytest -v

# Opt-in: the real client against the lab Windows DHCP server named in dev/env/lab.env (see tests/lab/conftest.py).
# Creates and removes nbwd-test-* objects in 198.18.0.0/15 and 2001:db8:4e42::/64 only.
test-lab:
	WINDOWS_DHCP_LAB=1 uv run pytest -v -p no:cacheprovider tests/lab

# Opt-in: Windows -> NetBox -> Windows on the dev stack against the lab server (see dev/scripts/lab_e2e.py).
# The first line runs on the host: the script itself runs in the dev container, where the tracked dev/env/netbox.env
# is not mounted, so it could not see a lab secret typed there.
lab-e2e:
	@uv run python -c "import sys, tests.lab.labkit as k; sys.tracebacklimit = 0; k.check_tracked_env()"
	$(EXEC) netbox $(PY) shell -c "exec(open('/plugin/dev/scripts/lab_e2e.py').read())"

# Opt-in: a realistic configuration on the two lab servers, to import into NetBox (see dev/scripts/lab_seed.py).
lab-seed:
	uv run python dev/scripts/lab_seed.py seed

lab-wipe:
	uv run python dev/scripts/lab_seed.py wipe

BASELINE = netbox_windows_dhcp_plugin/tests/query_counts.json
BRANCHING_BASELINE = tests_netbox/query_counts.branching.json

# The dev stack runs in branching mode, which adds queries to every page. Like CI, put the branching baselines in
# place for the run, then put the standalone file back. The restore runs from a shell trap, so an interrupted run
# (Ctrl-C, closed terminal) cleans up as well. A leftover .standalone file means an earlier run was killed before it
# could restore: starting again would overwrite the only copy of the standalone baseline, so the target refuses.
# The container mounts the plugin read-only, so this target cannot refresh a baseline: see docs/development.md.
test-netbox:
	@if [ -e $(BASELINE).standalone ]; then \
		echo "$(BASELINE).standalone exists: an earlier run was interrupted (or one is still running)."; \
		echo "Put it back first: mv $(BASELINE).standalone $(BASELINE)"; \
		exit 1; \
	fi
	@cp $(BASELINE) $(BASELINE).standalone || exit 1; \
	trap 'mv -f $(BASELINE).standalone $(BASELINE)' EXIT; trap 'exit 130' INT TERM HUP; \
	cp $(BRANCHING_BASELINE) $(BASELINE) && \
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

# The build backend (hatchling) and twine come from the `release` group in uv.lock, so a build uses the versions
# and hashes recorded there instead of whatever PyPI serves that day. CI and the release workflow run this target.
BUILD_CONSTRAINTS = build/build-constraints.txt

dist:
	rm -rf dist
	mkdir -p build
	uv export --quiet --locked --only-group release --no-emit-project -o $(BUILD_CONSTRAINTS)
	uv build --build-constraints $(BUILD_CONSTRAINTS) --require-hashes
	uv run --locked --isolated --only-group release twine check --strict dist/*
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
