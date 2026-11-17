"""The max_concurrency setting (issue #99): a bad value is logged and the default used."""

import logging

import pytest

from netbox_windows_dhcp_plugin import conf


@pytest.mark.parametrize("value", [1, 4, 10, 32])
def test_a_whole_number_of_at_least_one_is_used(monkeypatch, value):
    monkeypatch.setattr(conf, "get_setting", lambda key: value)
    assert conf.max_concurrency() == value


@pytest.mark.parametrize("value", [0, -2, None, "4", 2.5, True, False])
def test_anything_else_falls_back_to_the_default(monkeypatch, caplog, value):
    monkeypatch.setattr(conf, "get_setting", lambda key: value)
    with caplog.at_level(logging.ERROR, logger=conf.__name__):
        assert conf.max_concurrency() == conf.DEFAULTS["max_concurrency"] == 4
    assert f"max_concurrency must be a whole number of at least 1, not {value!r}: using 4" in caplog.text
