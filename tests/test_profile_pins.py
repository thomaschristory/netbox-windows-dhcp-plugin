"""
Credential profile pins: transports, ports, Kerberos realms and SSH host keys (issue #103), and
require_verified_https (issue #147). No NetBox needed.
"""

from __future__ import annotations

import base64
import struct

import pytest

from netbox_windows_dhcp_plugin import conf
from netbox_windows_dhcp_plugin.conf import CredentialError, connection_pin_errors, ssh_host_key_blob


def _key(key_type: str, body: bytes = b"key-material") -> str:
    blob = struct.pack(">I", len(key_type)) + key_type.encode() + body
    return f"{key_type} {base64.b64encode(blob).decode()}"


ED = _key("ssh-ed25519", b"one")
ED2 = _key("ssh-ed25519", b"two")
RSA = _key("ssh-rsa", b"three")


def errors(profile, **server):
    base = {
        "transport": "winrm",
        "port": 5986,
        "winrm_auth": "ntlm",
        "kerberos_realm": "",
        "ssh_host_key": "",
        "https": True,
        "verify_tls": True,
    }
    base.update(server)
    return connection_pin_errors("p", profile, **base)


def test_profile_without_pins_allows_everything():
    assert errors({}) == {}
    assert errors({}, transport="ssh", port=22, ssh_host_key=ED) == {}
    assert errors({}, winrm_auth="kerberos", kerberos_realm="ANY.EXAMPLE") == {}


def test_transports_matches_the_choice_values():
    assert conf.TRANSPORTS == ("winrm", "ssh")


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        ({"transports": "winrm"}, "transports must be a list"),
        ({"transports": None}, "transports must be a list"),
        ({"transports": ["WinRM"]}, "invalid entry 'WinRM'"),
        ({"transports": ["telnet"]}, "invalid entry 'telnet'"),
        ({"ports": 5986}, "ports must be a list"),
        ({"ports": "5986"}, "ports must be a list"),
        ({"ports": ["5986"]}, "invalid entry '5986'"),
        ({"ports": [True]}, "invalid entry True"),
        ({"ports": [0]}, "invalid entry 0"),
        ({"ports": [65536]}, "invalid entry 65536"),
        ({"ports": [5986.0]}, "invalid entry 5986.0"),
        ({"kerberos_realms": "CORP.EXAMPLE"}, "kerberos_realms must be a list"),
        ({"kerberos_realms": [""]}, "invalid entry ''"),
        ({"kerberos_realms": [None]}, "invalid entry None"),
        ({"ssh_host_keys": ED}, "ssh_host_keys must be a list"),
        ({"ssh_host_keys": ["ssh-ed25519 not-base64!"]}, "invalid entry"),
        ({"ssh_host_keys": ["ssh-ed25519"]}, "invalid entry"),
        # A key pasted under another type's label.
        ({"ssh_host_keys": [ED.replace("ssh-ed25519", "ssh-rsa", 1)]}, "invalid entry"),
        # Well formed, but a type the SSH transport cannot load: it would only fail when connecting.
        ({"ssh_host_keys": [_key("ssh-dss")]}, "invalid entry"),
        ({"ssh_host_keys": [_key("sk-ssh-ed25519@openssh.com")]}, "invalid entry"),
        ({"ssh_host_keys": [_key("ssh-foo")]}, "invalid entry"),
        # Only a real boolean: "false" or 0 would read one way or the other, None is an unset environment variable.
        ({"require_verified_https": "true"}, "require_verified_https must be true or false"),
        ({"require_verified_https": "false"}, "require_verified_https must be true or false"),
        ({"require_verified_https": 1}, "require_verified_https must be true or false"),
        ({"require_verified_https": 0}, "require_verified_https must be true or false"),
        ({"require_verified_https": None}, "require_verified_https must be true or false"),
        ({"require_verified_https": []}, "require_verified_https must be true or false"),
    ],
)
def test_malformed_profiles_are_refused(profile, message):
    with pytest.raises(CredentialError, match=message) as info:
        errors(profile, transport="ssh", port=22)
    assert "'p'" in str(info.value)


def test_transports():
    assert errors({"transports": ["winrm"]}) == {}
    assert errors({"transports": ["winrm"]}, transport="ssh", port=22) == {
        "transport": "credential profile 'p' may not be used over ssh"
    }
    assert errors({"transports": ("winrm", "ssh")}, transport="ssh", port=22) == {}
    assert "transport" in errors({"transports": []})


def test_ports():
    assert errors({"ports": [5986]}) == {}
    assert errors({"ports": [5986]}, port=5985) == {"port": "credential profile 'p' may not be used with port 5985"}
    assert "port" in errors({"ports": []})
    # An unknown transport has no port; the model reports the transport instead.
    assert errors({"ports": [5986]}, port=None) == {}


def test_kerberos_realms_only_apply_to_winrm_kerberos():
    profile = {"kerberos_realms": ["corp.example"]}
    assert errors(profile, winrm_auth="kerberos", kerberos_realm="CORP.EXAMPLE") == {}
    assert errors(profile, winrm_auth="kerberos", kerberos_realm="Corp.Example ") == {}
    assert errors(profile, winrm_auth="kerberos", kerberos_realm="EVIL.EXAMPLE") == {
        "kerberos_realm": "credential profile 'p' may not be used with Kerberos realm EVIL.EXAMPLE"
    }
    # No realm on the server would leave it to the worker's krb5.conf or the user name: refused too.
    assert errors(profile, winrm_auth="kerberos", kerberos_realm=" ") == {
        "kerberos_realm": "credential profile 'p' may not be used with Kerberos realm (none)"
    }
    # NTLM, CredSSP and SSH do not use the server's realm.
    assert errors(profile, winrm_auth="ntlm", kerberos_realm="EVIL.EXAMPLE") == {}
    assert errors(profile, winrm_auth="credssp", kerberos_realm="EVIL.EXAMPLE") == {}
    assert errors(profile, transport="ssh", port=22, winrm_auth="kerberos", kerberos_realm="EVIL.EXAMPLE") == {}
    assert "kerberos_realm" in errors({"kerberos_realms": []}, winrm_auth="kerberos", kerberos_realm="CORP.EXAMPLE")


def test_ssh_host_keys():
    profile = {"ssh_host_keys": [ED, RSA]}
    ssh = {"transport": "ssh", "port": 22}
    assert errors(profile, **ssh) == {}  # no key on the server: the transport trusts only the profile's
    assert errors(profile, **ssh, ssh_host_key=ED) == {}
    # Another comment, other spacing, a known_hosts host pattern: still the same key.
    assert errors(profile, **ssh, ssh_host_key=f"  dhcp01.example.com  {RSA}   a comment ") == {}
    assert errors(profile, **ssh, ssh_host_key=ED2) == {
        "ssh_host_key": "credential profile 'p' does not trust this SSH host key"
    }
    for bad in ("garbage", _key("ssh-dss")):
        assert "not a valid SSH host key" in errors(profile, **ssh, ssh_host_key=bad)["ssh_host_key"]
    assert errors({"ssh_host_keys": []}, **ssh) == {"ssh_host_key": "credential profile 'p' trusts no SSH host key"}
    # WinRM does not use host keys.
    assert errors(profile, ssh_host_key=ED2) == {}


def test_require_verified_https():
    on = {"require_verified_https": True}
    # Absent or false: today's behaviour.
    for profile in ({}, {"require_verified_https": False}):
        assert errors(profile, https=False, verify_tls=False) == {}
    assert errors(on) == {}
    # Whatever the logon: NTLM and Kerberos do not need HTTPS on their own.
    for auth in ("ntlm", "kerberos", "basic", "credssp"):
        assert errors(on, winrm_auth=auth, kerberos_realm="CORP.EXAMPLE", https=False, port=5985) == {
            "winrm_https": "credential profile 'p' sets require_verified_https: WinRM must use HTTPS, not HTTP"
        }
        assert errors(on, winrm_auth=auth, kerberos_realm="CORP.EXAMPLE", verify_tls=False) == {
            "verify_tls": "credential profile 'p' sets require_verified_https: WinRM must verify the server's "
            "certificate"
        }
    # Plain HTTP is reported first; verification means nothing without HTTPS.
    assert set(errors(on, https=False, verify_tls=False)) == {"winrm_https"}
    # SSH needs the profile's own host keys: otherwise an editor could switch to SSH and pin a key they chose.
    ssh = {"transport": "ssh", "port": 22, "https": False, "verify_tls": False}
    assert errors(on, **ssh) == {
        "transport": "credential profile 'p' sets require_verified_https, which needs ssh_host_keys for SSH "
        '(or transports: ["winrm"])'
    }
    assert errors({**on, "ssh_host_keys": [ED]}, **ssh) == {}
    assert errors({**on, "ssh_host_keys": [ED]}, **ssh, ssh_host_key=ED2) == {
        "ssh_host_key": "credential profile 'p' does not trust this SSH host key"
    }
    # When transports already refuses SSH, that message stays.
    assert errors({**on, "transports": ["winrm"]}, **ssh) == {
        "transport": "credential profile 'p' may not be used over ssh"
    }
    # Without the key, SSH is as before.
    assert errors({}, **ssh) == {}


def test_profile_require_verified_https():
    assert conf.profile_require_verified_https("p", {}) is False
    assert conf.profile_require_verified_https("p", {"require_verified_https": False}) is False
    assert conf.profile_require_verified_https("p", {"require_verified_https": True}) is True
    with pytest.raises(CredentialError, match="'p': require_verified_https must be true or false"):
        conf.profile_require_verified_https("p", {"require_verified_https": "yes"})


def test_several_errors_are_reported_together():
    profile = {"transports": ["winrm"], "ports": [5986], "ssh_host_keys": [ED]}
    assert set(errors(profile, transport="ssh", port=22, ssh_host_key=ED2)) == {"transport", "port", "ssh_host_key"}
    profile = {"transports": ["ssh"], "ports": [5986], "require_verified_https": True}
    assert set(errors(profile, port=5985, https=False)) == {"transport", "port", "winrm_https"}


def test_ssh_host_key_types():
    for key_type in conf.SSH_HOST_KEY_TYPES:
        assert ssh_host_key_blob(_key(key_type)) is not None


def test_ssh_host_key_blob():
    assert ssh_host_key_blob(ED) == ssh_host_key_blob(f"{ED} comment") == ssh_host_key_blob(f"host {ED} c")
    assert ssh_host_key_blob(ED) != ssh_host_key_blob(ED2)
    for bad in (None, 3, "", "ssh-ed25519", "ssh-ed25519 ###", "ssh-ed25519 AAAA", _key("ssh-dss")):
        assert ssh_host_key_blob(bad) is None


def test_profile_ssh_host_keys_returns_the_keys_as_given():
    assert conf.profile_ssh_host_keys("p", {"ssh_host_keys": [f" {ED} c "]}) == [f"{ED} c"]
    assert conf.profile_ssh_host_keys("p", {}) is None


def test_profile_keys_are_listed_once():
    assert "require_verified_https" in conf.PROFILE_KEYS
    assert {"password", "password_env", "password_file", "secret_file"} <= conf.PROFILE_KEYS


def test_unknown_profile_keys_are_reported():
    profiles = {
        "ok": {"username": "u", "password_env": "X", "require_verified_https": True, "ssh_host_keys": [ED]},
        "typo": {"username": "u", "require_verified_http": True, "allowed_host": ["a"]},
    }
    assert conf.profiles_with_unknown_keys(profiles) == {"typo": ["allowed_host", "require_verified_http"]}
    assert conf.profiles_with_unknown_keys({}) == {}
