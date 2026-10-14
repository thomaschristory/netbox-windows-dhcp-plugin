"""Checks on the user-facing docs: style, navigation, links, and that they match the plugin's settings."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from netbox_windows_dhcp_plugin import conf

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
# Keys a credential profile may hold: each secret inline, as <key>_env or <key>_file, plus allowed_hosts.
SECRET_KEYS = ("username", "password", "private_key", "secret")
PROFILE_KEYS = {"allowed_hosts"} | {f"{k}{suffix}" for k in SECRET_KEYS for suffix in ("", "_env", "_file")}


def doc_pages() -> list[Path]:
    return sorted(DOCS.rglob("*.md"))


def all_pages() -> list[Path]:
    return [*doc_pages(), ROOT / "README.md", ROOT / "CHANGELOG.md"]


def _id(page: Path) -> str:
    return str(page.relative_to(ROOT))


@pytest.mark.parametrize("page", all_pages(), ids=_id)
def test_no_em_dash(page):
    assert "—" not in page.read_text()


def test_every_page_is_in_the_navigation():
    nav = set(re.findall(r":\s+(\S+\.md)\s*$", (ROOT / "mkdocs.yml").read_text(), flags=re.M))
    pages = {str(p.relative_to(DOCS)) for p in doc_pages()}
    assert nav == pages


def _slug(heading: str) -> str:
    # Python-Markdown's toc slugify: drop markup and punctuation, lowercase, spaces to hyphens.
    text = re.sub(r"[`*]", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def _anchors(page: Path) -> set[str]:
    text = re.sub(r"^```.*?^```", "", page.read_text(), flags=re.S | re.M)
    return {_slug(h) for h in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, flags=re.M)}


def _links():
    for page in doc_pages():
        text = re.sub(r"^```.*?^```", "", page.read_text(), flags=re.S | re.M)
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if not target.startswith(("http://", "https://", "mailto:")):
                yield pytest.param(page, target, id=f"{_id(page)}->{target}")


@pytest.mark.parametrize(("page", "target"), list(_links()))
def test_internal_links_resolve(page, target):
    path, _, anchor = target.partition("#")
    dest = (page.parent / path).resolve() if path else page
    assert dest.is_file(), f"{target}: no such page"
    if anchor:
        assert anchor in _anchors(dest), f"{target}: no such heading"


ROW = re.compile(r"^\| `([a-z_]+)` \| `([^`]*)` \|", flags=re.M)


def test_configuration_reference_matches_defaults():
    rows = ROW.findall((DOCS / "configuration.md").read_text())
    documented = {key: ast.literal_eval(default) for key, default in rows}
    assert len(rows) == len(documented)
    assert documented == conf.DEFAULTS


def _config_snippets():
    for page in all_pages():
        for index, block in enumerate(re.findall(r"^```python\n(.*?)^```", page.read_text(), flags=re.S | re.M)):
            if "PLUGINS_CONFIG" in block:
                yield pytest.param(block, id=f"{_id(page)}#{index}")


@pytest.mark.parametrize("block", list(_config_snippets()))
def test_config_snippets_are_valid(block):
    namespace: dict = {}
    exec(compile(block, "<snippet>", "exec"), namespace)
    settings = namespace["PLUGINS_CONFIG"]["netbox_windows_dhcp_plugin"]
    assert set(settings) <= set(conf.DEFAULTS)
    for name, profile in settings.get("credentials", {}).items():
        assert set(profile) <= PROFILE_KEYS, name
        # The docs never show a profile without allowed_hosts: NetBox warns about those.
        assert isinstance(profile.get("allowed_hosts"), list), name
        for host in profile["allowed_hosts"]:
            assert conf.normalise_host(host.replace("*", "x").replace("?", "x")), host


def test_docs_build_input_is_tracked():
    # make docs stages exactly these files for Zensical, which publishes everything under docs_dir.
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "docs", "mkdocs.yml", "CHANGELOG.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "docs/index.md" in listed
    assert "mkdocs.yml" in listed
