"""Regression tests for the transport findings of the code quality audit (group g1-transport)."""

from django.test import TestCase, override_settings

from netbox_windows_dhcp_plugin.models import FailoverRelationship

from .test_jobs import CREDENTIALS
from .test_remaining import RemainingObjectsMixin


def config(**lab):
    profiles = {"lab": {"username": "lab\\admin", "password": "x", **lab}}
    return {**CREDENTIALS, "netbox_windows_dhcp_plugin": {"credentials": profiles}}


@override_settings(PLUGINS_CONFIG=config())
class FailoverSecretInlineNoneTest(RemainingObjectsMixin, TestCase):
    """An inline None stops a job only for the key that job needs (finding R18 on F11)."""

    def test_a_none_secret_is_reported_when_a_relationship_needs_it(self):
        from netbox_windows_dhcp_plugin.connection import failover_secret_lookup

        FailoverRelationship.objects.filter(primary_server=self.server).update(shared_secret_profile="lab")
        lookup = failover_secret_lookup(self.server)
        with (
            override_settings(PLUGINS_CONFIG=config(secret=None)),
            self.assertRaisesMessage(ValueError, "failover HA: credential profile 'lab': secret is set to None"),
        ):
            lookup("HA")
        # A profile read only for its secret is not stopped by a None it does not use.
        with override_settings(PLUGINS_CONFIG=config(secret="s3", private_key=None)):
            self.assertEqual(lookup("HA"), "s3")

    def test_a_none_secret_does_not_stop_a_connection_that_never_uses_it(self):
        from netbox_windows_dhcp_plugin.conf import get_credentials

        with override_settings(PLUGINS_CONFIG=config(secret=None, private_key=None)):
            creds = get_credentials("lab")
        self.assertEqual((creds.password, creds.secret, creds.private_key), ("x", "", ""))
