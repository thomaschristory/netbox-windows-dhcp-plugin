"""The credential profile "proxy" key (issue #105). No NetBox needed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from netbox_windows_dhcp_plugin import conf
from netbox_windows_dhcp_plugin.conf import CredentialError, check_secret_connection, connection_pin_errors
from netbox_windows_dhcp_plugin.windows import winrm


def test_values_match_the_winrm_transport():
    assert conf.PROXIES == winrm.PROXIES
    assert (conf.PROXY_NONE, conf.PROXY_ENV) == (winrm.PROXY_NONE, winrm.PROXY_ENV)


def test_absent_means_direct():
    assert conf.profile_proxy("p", {}) == "none"
    assert conf.profile_proxy("p", {"proxy": "none"}) == "none"
    assert conf.profile_proxy("p", {"proxy": "env"}) == "env"


@pytest.mark.parametrize("value", [None, True, False, "", "ENV", "Env", "system", "http://proxy:3128", ["env"]])
def test_anything_else_is_refused(value):
    with pytest.raises(CredentialError, match="proxy must be"):
        conf.profile_proxy("p", {"proxy": value})


def test_pin_check_refuses_a_malformed_proxy_for_any_transport():
    # A typo is reported wherever the profile is checked (server save, connection, failover secret), even on a
    # profile used over SSH, which never uses a proxy.
    for transport in ("winrm", "ssh"):
        with pytest.raises(CredentialError, match="proxy must be"):
            connection_pin_errors(
                "p",
                {"proxy": "yes"},
                transport=transport,
                port=5986,
                winrm_auth="ntlm",
                kerberos_realm="",
                ssh_host_key="",
                https=True,
                verify_tls=True,
            )


def _server(**kw):
    base = {
        "credential_profile": "srv",
        "transport": "winrm",
        "effective_port": 5986,
        "winrm_auth": "ntlm",
        "failover_auth": "",
        "kerberos_realm": "",
        "ssh_host_key": "",
        "winrm_https": True,
        "verify_tls": True,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("server_proxy", "secret_proxy", "allowed"),
    [
        (None, None, True),
        ("none", None, True),
        (None, "env", True),
        ("env", "env", True),
        ("env", None, False),
        ("env", "none", False),
    ],
)
def test_failover_secret_needs_consent_to_travel_through_the_proxy(server_proxy, secret_proxy, allowed):
    profiles = {"srv": {}, "ha": {"secret": "s"}}
    if server_proxy:
        profiles["srv"]["proxy"] = server_proxy
    if secret_proxy:
        profiles["ha"]["proxy"] = secret_proxy
    if allowed:
        check_secret_connection("ha", _server(), profiles=profiles)
    else:
        with pytest.raises(CredentialError, match='does not set proxy "env"'):
            check_secret_connection("ha", _server(), profiles=profiles)


def test_failover_secret_over_ssh_ignores_the_proxy_key():
    profiles = {"srv": {"proxy": "env"}, "ha": {"secret": "s"}}
    check_secret_connection("ha", _server(transport="ssh", effective_port=22), profiles=profiles)


def test_failover_secret_from_the_server_profile_itself_is_not_checked_twice():
    profiles = {"srv": {"proxy": "env", "secret": "s"}}
    check_secret_connection("srv", _server(), profiles=profiles)


SECRET_URL = "http://svc:S3cret-pw@proxy.example.com:3128"


def test_a_proxy_url_never_appears_in_the_message():
    with pytest.raises(CredentialError) as ctx:
        conf.profile_proxy("p", {"proxy": SECRET_URL})
    message = str(ctx.value)
    assert "S3cret-pw" not in message and "svc" not in message and "proxy.example.com" not in message
    assert "a proxy URL is not accepted" in message


def test_a_proxy_url_never_appears_in_the_failover_check():
    profiles = {"srv": {"proxy": "env"}, "ha": {"secret": "s", "proxy": SECRET_URL}}
    with pytest.raises(CredentialError) as ctx:
        check_secret_connection("ha", _server(), profiles=profiles)
    assert "S3cret-pw" not in str(ctx.value) and "proxy.example.com" not in str(ctx.value)


def test_a_malformed_server_profile_is_reported_as_the_server_profile():
    profiles = {"srv": {"proxy": SECRET_URL}, "ha": {"secret": "s"}}
    with pytest.raises(conf.ServerProfileError, match="credential profile 'srv'") as ctx:
        check_secret_connection("ha", _server(), profiles=profiles)
    assert "S3cret-pw" not in str(ctx.value)


def test_proxy_is_a_known_profile_key():
    # NetBox warns at startup about keys it does not know; "proxy" must not be one of them.
    assert "proxy" in conf.PROFILE_KEYS
    assert conf.profiles_with_unknown_keys({"p": {"username": "u", "proxy": "env"}}) == {}


def test_proxy_does_not_trigger_the_startup_warning(caplog):
    with caplog.at_level("WARNING"):
        conf.warn_unknown_profile_keys({"p": {"username": "u", "proxy": "env"}})
    assert caplog.records == []


def test_verified_https_through_a_proxy_is_allowed():
    # require_verified_https is about TLS to the DHCP server; a proxy then only tunnels it, so the two combine.
    profile = {"require_verified_https": True, "proxy": "env"}
    common = {"transport": "winrm", "port": 5986, "winrm_auth": "ntlm", "kerberos_realm": "", "ssh_host_key": ""}
    assert connection_pin_errors("p", profile, https=True, verify_tls=True, **common) == {}
    assert set(connection_pin_errors("p", profile, https=False, verify_tls=True, **common)) == {"winrm_https"}
    assert set(connection_pin_errors("p", profile, https=True, verify_tls=False, **common)) == {"verify_tls"}


def test_a_malformed_proxy_is_reported_before_the_https_check():
    with pytest.raises(CredentialError, match="proxy must be"):
        connection_pin_errors(
            "p",
            {"require_verified_https": True, "proxy": "ENV"},
            transport="winrm",
            port=5985,
            winrm_auth="ntlm",
            kerberos_realm="",
            ssh_host_key="",
            https=False,
            verify_tls=False,
        )
