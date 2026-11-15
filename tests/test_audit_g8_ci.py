"""Checks on the CI, release and dev tooling files (code quality audit, group g8-ci).

These read the files as text: a workflow cannot be run from a unit test, but the properties below are easy to lose
in an edit and each one closes a hole the audit found.
"""

from __future__ import annotations

import re
import runpy
import sys
import tomllib
import types
import warnings
from pathlib import Path

import pytest

from tests.lab import labkit

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"


def read(*parts: str) -> str:
    return REPO.joinpath(*parts).read_text()


def job(workflow: str, name: str) -> str:
    """The text of one job: from its two-space key to the next one."""
    text = (WORKFLOWS / workflow).read_text()
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text, re.S | re.M)
    assert match, f"no job {name} in {workflow}"
    return match.group(1)


# -- F01: superseded pull request runs are cancelled, runs on main never are -----------------------------------------


def test_ci_cancels_superseded_pull_request_runs_only():
    text = read(".github", "workflows", "ci.yml")
    assert "\nconcurrency:\n" in text
    # One group per pull request; every other run (main, by hand) has a group of its own, so none is dropped.
    assert "group: ci-${{ github.event.pull_request.number || github.sha }}" in text
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text


# -- F02: a tag is not published before lint and the unit tests pass -------------------------------------------------


def test_release_publish_waits_for_lint_and_unit_tests():
    test_job = job("release.yml", "test")
    for command in ("uv sync --locked", "uv run ruff check .", "uv run pytest"):
        assert command in test_job
    assert "needs: [build, test]" in job("release.yml", "publish-pypi")


def test_release_jobs_have_timeouts():
    for name in ("test", "build", "publish-pypi"):
        assert "timeout-minutes:" in job("release.yml", name), name


# -- F03: the build backend and twine come from uv.lock --------------------------------------------------------------


def test_build_tools_are_locked():
    groups = tomllib.loads(read("pyproject.toml"))["dependency-groups"]
    names = {re.split(r"[<>=!~ \[]", requirement, maxsplit=1)[0] for requirement in groups["release"]}
    assert names == {"hatchling", "twine"}
    locked = {package["name"] for package in tomllib.loads(read("uv.lock"))["package"]}
    assert {"hatchling", "twine"} <= locked


def test_dist_target_builds_from_the_lock():
    makefile = read("Makefile")
    assert "uv export --quiet --locked --only-group release" in makefile
    assert "uv build --build-constraints $(BUILD_CONSTRAINTS) --require-hashes" in makefile
    assert "uv run --locked --isolated --only-group release twine check --strict dist/*" in makefile


@pytest.mark.parametrize("path", ["Makefile", ".github/workflows/ci.yml", ".github/workflows/release.yml"])
def test_no_unpinned_build_commands(path):
    text = read(path)
    assert "uvx " not in text
    # A bare `uv build` would fetch the newest hatchling at build time.
    for line in text.splitlines():
        if "uv build" in line and not line.lstrip().startswith("#"):
            assert "--build-constraints" in line and "--require-hashes" in line, line


def test_ci_and_release_build_with_the_same_target():
    assert "run: make dist" in job("ci.yml", "build")
    assert "run: make dist" in job("release.yml", "build")


# -- F04: no TestPyPI job left that fails on a second run and creates an unprotected environment ---------------------


def test_release_has_no_testpypi_job():
    text = read(".github", "workflows", "release.yml")
    assert "testpypi" not in text.lower()
    # Only the tag path may ask for an identity token.
    assert text.count("id-token: write") == 1
    assert "if: github.event_name == 'push' && github.ref_type == 'tag'" in job("release.yml", "publish-pypi")


# -- F05: make test-netbox cannot lose the standalone baseline -------------------------------------------------------


def test_test_netbox_guards_and_restores_the_baseline():
    makefile = read("Makefile")
    recipe = makefile[makefile.index("\ntest-netbox:\n") : makefile.index("\nlint:\n")]
    assert "if [ -e $(BASELINE).standalone ]" in recipe
    assert "trap 'mv -f $(BASELINE).standalone $(BASELINE)' EXIT" in recipe
    # A closed terminal sends HUP: without it in the list the shell dies before the EXIT trap runs.
    assert "trap 'exit 130' INT TERM HUP" in recipe
    # The plugin is mounted read-only in the container, so the old "refresh the branching file" step did nothing.
    assert "UPDATE_QUERY_COUNTS" not in recipe
    assert "$(BRANCHING_BASELINE)" in recipe and "cp $(BASELINE) $(BRANCHING_BASELINE)" not in recipe


# -- F06: the dev stack listens on loopback only, and the lab profile can be limited to the lab hosts ----------------


def test_dev_stack_only_publishes_on_loopback():
    compose = read("dev", "docker-compose.yml")
    ports = [line.strip() for line in compose.splitlines() if line.strip().startswith("ports:")]
    assert ports == ['ports: ["127.0.0.1:8000:8080"]']


def dev_plugins_config(monkeypatch, allowed: str | None) -> dict:
    """Run dev/configuration/plugins.py with a stand-in for netbox-branching, which the unit tier does not have."""
    utilities = types.ModuleType("netbox_branching.utilities")
    utilities.DynamicSchemaDict = dict
    monkeypatch.setitem(sys.modules, "netbox_branching", types.ModuleType("netbox_branching"))
    monkeypatch.setitem(sys.modules, "netbox_branching.utilities", utilities)
    if allowed is None:
        monkeypatch.delenv("WINDOWS_DHCP_DEV_ALLOWED_HOSTS", raising=False)
    else:
        monkeypatch.setenv("WINDOWS_DHCP_DEV_ALLOWED_HOSTS", allowed)
    settings = runpy.run_path(str(REPO / "dev" / "configuration" / "plugins.py"))
    return settings["PLUGINS_CONFIG"]["netbox_windows_dhcp_plugin"]["credentials"]


def test_dev_lab_profile_takes_allowed_hosts_from_the_environment(monkeypatch):
    from netbox_windows_dhcp_plugin.conf import host_allowed

    lab = dev_plugins_config(monkeypatch, " 10.20.0.*, dhcp01.example.com ,")["lab"]
    assert lab["allowed_hosts"] == ["10.20.0.*", "dhcp01.example.com"]
    assert host_allowed("lab", lab, "10.20.0.11")
    assert host_allowed("lab", lab, "dhcp01.example.com")
    assert not host_allowed("lab", lab, "attacker.example.net")


@pytest.mark.parametrize("allowed", [None, "", " , "])
def test_dev_lab_profile_is_unrestricted_without_the_variable(monkeypatch, allowed):
    # The NetBox tests create servers with made-up hosts under the profile name "lab": an empty list would
    # refuse them all, so the key is left out instead.
    lab = dev_plugins_config(monkeypatch, allowed)["lab"]
    assert "allowed_hosts" not in lab
    assert lab["username_env"] == "WINDOWS_DHCP_USERNAME" and lab["password_env"] == "WINDOWS_DHCP_PASSWORD"


# -- F87: runtime dependencies are written down once -----------------------------------------------------------------


def test_dev_group_does_not_repeat_runtime_dependencies():
    project = tomllib.loads(read("pyproject.toml"))
    dev = " ".join(project["dependency-groups"]["dev"])
    assert "pypsrp" not in dev and "paramiko" not in dev
    # The floors the package promises are untouched.
    assert project["project"]["dependencies"] == ["pypsrp[credssp]>=0.9.1", "paramiko>=3.5"]


def test_dev_image_installs_the_branching_extra():
    dockerfile = read("dev", "Dockerfile")
    assert '-e "/plugin[branching]"' in dockerfile
    assert "netboxlabs-netbox-branching>=" not in dockerfile and "netbox-change-control>=" not in dockerfile


# -- F88: lab settings are read from the gitignored file only --------------------------------------------------------


def test_tracked_netbox_env_holds_no_lab_settings():
    keys = [line.split("=", 1)[0].strip() for line in read("dev", "env", "netbox.env").splitlines()]
    assert not [key for key in keys if key.startswith("WINDOWS_DHCP_")]


def test_labkit_reads_lab_env_only():
    assert labkit.ENV_FILES == (REPO / "dev" / "env" / "lab.env",)
    assert labkit.TRACKED_ENV_FILE == REPO / "dev" / "env" / "netbox.env"


@pytest.fixture
def env_files(tmp_path, monkeypatch):
    tracked, ignored = tmp_path / "netbox.env", tmp_path / "lab.env"
    monkeypatch.setattr(labkit, "ENV_FILES", (ignored,))
    monkeypatch.setattr(labkit, "TRACKED_ENV_FILE", tracked)
    for key in [key for key in labkit.os.environ if key.startswith("WINDOWS_DHCP_")]:
        monkeypatch.delenv(key)
    return tracked, ignored


def test_lab_env_ignores_settings_in_the_tracked_file(env_files):
    tracked, ignored = env_files
    tracked.write_text("SUPERUSER_NAME=admin\nWINDOWS_DHCP_LAB_HOST=old.example.com\nWINDOWS_DHCP_USERNAME=old\n")
    ignored.write_text("# lab\nWINDOWS_DHCP_LAB_HOST='dhcp01.example.com'\nOTHER=1\n")
    assert labkit.lab_env() == {"WINDOWS_DHCP_LAB_HOST": "dhcp01.example.com"}


def test_lab_env_works_without_either_file(env_files):
    assert labkit.lab_env() == {}


@pytest.mark.parametrize("key", ["WINDOWS_DHCP_PASSWORD", "WINDOWS_DHCP_LAB_FAILOVER_SECRET"])
def test_lab_env_refuses_a_secret_in_the_tracked_file(env_files, key):
    tracked, ignored = env_files
    tracked.write_text(f"{key}=hunter2-not-real\n")
    ignored.write_text("WINDOWS_DHCP_LAB_HOST=dhcp01.example.com\n")
    with pytest.raises(labkit.TrackedSecretError) as raised:
        labkit.lab_env()
    assert key in str(raised.value) and "lab.env" in str(raised.value)
    # The message names the key, never the value.
    assert "hunter2" not in str(raised.value)


def test_lab_env_accepts_an_empty_placeholder_in_the_tracked_file(env_files):
    tracked, _ = env_files
    tracked.write_text("WINDOWS_DHCP_PASSWORD=\nWINDOWS_DHCP_LAB_FAILOVER_SECRET=''\n")
    assert labkit.lab_env() == {}


# -- R15: a secret in the tracked file never stops the unit run ------------------------------------------------------


LAB_MODULE = REPO / "tests" / "lab" / "test_lab_failover.py"


def test_lab_collection_is_untouched_without_a_secret_in_the_tracked_file(env_files, monkeypatch):
    from tests.lab import conftest as lab_conftest

    tracked, _ = env_files
    tracked.write_text("SUPERUSER_NAME=admin\nWINDOWS_DHCP_PASSWORD=\n")
    monkeypatch.delenv("WINDOWS_DHCP_LAB", raising=False)
    assert lab_conftest.pytest_ignore_collect(collection_path=LAB_MODULE, config=None) is None


def test_unit_run_warns_and_goes_on_with_a_secret_in_the_tracked_file(env_files, monkeypatch):
    from tests.lab import conftest as lab_conftest

    tracked, _ = env_files
    tracked.write_text("WINDOWS_DHCP_PASSWORD=hunter2-not-real\n")
    monkeypatch.delenv("WINDOWS_DHCP_LAB", raising=False)
    # `make test` collects tests/lab too. Lab modules that read the settings on import are left out, and the
    # later hook must not raise either, or no unit test runs at all.
    assert lab_conftest.pytest_ignore_collect(collection_path=LAB_MODULE, config=None) is True
    with pytest.warns(pytest.PytestWarning, match="WINDOWS_DHCP_PASSWORD") as caught:
        lab_conftest.pytest_collection_modifyitems(config=None, items=[])
    assert "lab.env" in str(caught[0].message) and "hunter2" not in str(caught[0].message)


def test_lab_run_stops_without_a_traceback_on_a_secret_in_the_tracked_file(env_files, monkeypatch):
    from tests.lab import conftest as lab_conftest

    tracked, _ = env_files
    tracked.write_text("WINDOWS_DHCP_LAB_FAILOVER_SECRET=hunter2-not-real\n")
    monkeypatch.setenv("WINDOWS_DHCP_LAB", "1")
    # pytest prints a CollectError as its message alone, where any other exception gets a full traceback.
    with pytest.raises(pytest.Collector.CollectError) as raised:
        lab_conftest.pytest_ignore_collect(collection_path=LAB_MODULE, config=None)
    assert raised.value.__suppress_context__
    assert "WINDOWS_DHCP_LAB_FAILOVER_SECRET" in str(raised.value) and "lab.env" in str(raised.value)
    assert "hunter2" not in str(raised.value)
    # The later hook adds nothing: no second report, no exception.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        lab_conftest.pytest_collection_modifyitems(config=None, items=[])


# -- R14, R21: the docs only promise the tracked-file check where it runs --------------------------------------------


def test_docs_do_not_promise_the_tracked_file_check_for_lab_e2e():
    makefile = read("Makefile")
    recipe = makefile[makefile.index("\nlab-e2e:\n") : makefile.index("\nlab-seed:\n")]
    if "check_tracked_env" in recipe:
        return  # checked on the host before the container runs: the promise holds for every lab target
    # lab-e2e runs in the dev container, where dev/env/netbox.env is not mounted but its values are in the
    # environment: the check finds nothing there, so the docs must not say it stops.
    docs = " ".join(read("docs", "development.md").split())
    assert "the lab targets stop with an error" not in docs
    assert "`make lab-e2e` runs inside the dev container, which still loads that file and has no such check" in docs
    changelog = " ".join(read("CHANGELOG.md").split())
    assert "and `make lab-e2e` read lab settings from `dev/env/lab.env` and refuse" not in changelog
    assert "`make lab-e2e` runs in the dev container and does not check" in changelog
