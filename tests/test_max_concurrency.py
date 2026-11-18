"""The max_concurrency setting (issue #99): a bad value is logged and the default used, a high one capped."""

import logging

import pytest

from netbox_windows_dhcp_plugin import conf


@pytest.fixture(autouse=True)
def fresh_log(monkeypatch):
    # Each message is logged once per process: start every test with none logged.
    monkeypatch.setattr(conf, "_concurrency_logged", set())


def setting(monkeypatch, value):
    monkeypatch.setattr(conf, "get_setting", lambda key: value)


@pytest.mark.parametrize("value", [1, 4, 10])
def test_a_whole_number_up_to_ten_is_used_quietly(monkeypatch, caplog, value):
    setting(monkeypatch, value)
    with caplog.at_level(logging.WARNING, logger=conf.__name__):
        assert conf.max_concurrency() == value
    assert caplog.records == []


@pytest.mark.parametrize("value", [0, -2, None, "4", 2.5, True, False])
def test_anything_else_falls_back_to_the_default(monkeypatch, caplog, value):
    setting(monkeypatch, value)
    with caplog.at_level(logging.ERROR, logger=conf.__name__):
        assert conf.max_concurrency() == conf.DEFAULTS["max_concurrency"] == 4
    assert f"max_concurrency must be a whole number of at least 1, not {value!r}: using 4" in caplog.text


def test_above_ten_is_used_with_a_warning(monkeypatch, caplog):
    setting(monkeypatch, 16)
    with caplog.at_level(logging.WARNING, logger=conf.__name__):
        assert conf.max_concurrency() == 16
    assert "max_concurrency 16 is above 10" in caplog.text and "MaxSessions" in caplog.text


def test_above_thirty_is_cut_down(monkeypatch, caplog):
    setting(monkeypatch, 100)
    with caplog.at_level(logging.WARNING, logger=conf.__name__):
        assert conf.max_concurrency() == 30
    assert "max_concurrency 100 is above 30 (the default WinRM MaxShellsPerUser): using 30" in caplog.text


def test_each_message_is_logged_once_per_value(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=conf.__name__):
        for value in ("x", "x", 0, 0, 50, 50, 12, 12, "x"):
            setting(monkeypatch, value)
            conf.max_concurrency()
    assert len(caplog.records) == 4
