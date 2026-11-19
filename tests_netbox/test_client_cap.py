"""Issue #99: the client a job builds caps commands in flight at max_concurrency, over WinRM and SSH alike.

The jobs pass max_concurrency to windows_state.read as its number of workers (test_parallel_reads.py), but the cap
that holds whatever the caller does is the Limited wrapper build_client puts around the transport. These tests check
that wrapper is there and sized from the same setting, so a read can never open more WinRM shells or SSH sessions
than the setting allows.
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from netbox_windows_dhcp_plugin import conf
from netbox_windows_dhcp_plugin.choices import TransportChoices
from netbox_windows_dhcp_plugin.connection import build_client
from netbox_windows_dhcp_plugin.models import WindowsServer
from netbox_windows_dhcp_plugin.windows.runner import Limited
from netbox_windows_dhcp_plugin.windows.ssh import SSHRunner
from netbox_windows_dhcp_plugin.windows.winrm import WinRMRunner

from .test_jobs import CREDENTIALS

PLUGIN = "netbox_windows_dhcp_plugin"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGbM5L1hQdQHwPVzFXDyOcnRTMUXN0zXV0fRSC0c0b7w"

# (setting, cap the client must carry). A value that is not a whole number of at least 1 falls back to the default
# of 4; above 30 (the default WinRM MaxShellsPerUser) it is cut down to 30.
CASES = [
    (None, 4),  # not set: the default
    (1, 1),
    (3, 3),
    (16, 16),
    (30, 30),
    (100, 30),
    (0, 4),
    (-1, 4),
    ("lots", 4),
    (2.5, 4),
    (True, 4),
]


def plugins_config(value):
    config = dict(CREDENTIALS[PLUGIN])
    if value is not None:
        config["max_concurrency"] = value
    return {**CREDENTIALS, PLUGIN: config}


def server(transport):
    # Not saved: build_client only reads the connection fields, and nothing here needs the database.
    if transport == TransportChoices.SSH:
        return WindowsServer(
            name="dhcp1", host="dhcp1.example.com", credential_profile="lab", transport=transport, ssh_host_key=HOST_KEY
        )
    return WindowsServer(name="dhcp1", host="dhcp1.example.com", credential_profile="lab", transport=transport)


class ClientCapTest(SimpleTestCase):
    def setUp(self):
        # The bad-value messages are logged once per process; start each test with none logged.
        patcher = mock.patch.object(conf, "_concurrency_logged", set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, transport, runner_class):
        for value, cap in CASES:
            with self.subTest(setting=value), override_settings(PLUGINS_CONFIG=plugins_config(value)):
                client = build_client(server(transport))
                self.assertIsInstance(client.runner, Limited)
                self.assertEqual(client.runner.n, cap)
                self.assertEqual(client.runner.n, conf.max_concurrency())
                # The semaphore really allows `cap` commands at once, not just the attribute.
                self.assertEqual(client.runner._sem._value, cap)
                # The cap wraps the real transport, built from the server's own settings.
                self.assertIsInstance(client.runner.runner, runner_class)
                client.close()

    def test_winrm_client_is_capped_at_max_concurrency(self):
        self.check(TransportChoices.WINRM, WinRMRunner)

    def test_ssh_client_is_capped_at_max_concurrency(self):
        self.check(TransportChoices.SSH, SSHRunner)

    @override_settings(PLUGINS_CONFIG=plugins_config(2))
    def test_a_substituted_runner_is_capped_too(self):
        # Tests and runner_factory hand in their own runner; it gets the same cap as a real transport.
        fake = object()
        client = build_client(server(TransportChoices.WINRM), runner=fake)
        self.assertIsInstance(client.runner, Limited)
        self.assertEqual(client.runner.n, 2)
        self.assertIs(client.runner.runner, fake)
