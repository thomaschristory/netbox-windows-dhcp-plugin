"""Build a Windows DHCP client for a WindowsServer, resolving its credential profile."""

from .choices import TransportChoices, WinRMAuthChoices
from .conf import CredentialError, check_host_allowed, get_credentials, get_setting
from .windows import DhcpClient, limit


def check_connection_allowed(server):
    """
    Refuse to send credentials where they could leak, even if the record skipped form validation (for example a
    host changed in the database, or a profile's allowed_hosts tightened after the server was saved).
    """
    # computer_name counts too: with delegation the first host passes the credentials on to it.
    check_host_allowed(server.credential_profile, server.host, server.computer_name)
    if (
        server.transport == TransportChoices.WINRM
        and server.winrm_auth == WinRMAuthChoices.BASIC
        and not (server.winrm_https and server.verify_tls)
    ):
        raise CredentialError("Basic authentication needs HTTPS with certificate verification")


def build_runner(server):
    check_connection_allowed(server)
    creds = get_credentials(server.credential_profile)
    timeout = get_setting("command_timeout")
    if server.transport == TransportChoices.SSH:
        from .windows.ssh import SSHConfig, SSHRunner

        return SSHRunner(
            SSHConfig(
                host=server.host,
                username=creds.username,
                password=creds.password,
                private_key=creds.private_key,
                port=server.effective_port,
                host_key=server.ssh_host_key,
                # Never skip the host key check: a machine in the middle would receive the password.
                insecure=False,
                command_timeout=timeout,
            )
        )
    from .windows.winrm import WinRMConfig, WinRMRunner

    return WinRMRunner(
        WinRMConfig(
            host=server.host,
            username=creds.username,
            password=creds.password,
            port=server.effective_port,
            https=server.winrm_https,
            cert_validation=server.verify_tls,
            auth=server.winrm_auth,
            kerberos_realm=server.kerberos_realm,
            kerberos_spn=server.kerberos_spn,
            timeout=timeout,
        )
    )


def build_client(server, runner=None):
    """Return a DhcpClient for the server. Pass `runner` to substitute a fake in tests."""
    check_connection_allowed(server)
    runner = runner if runner is not None else build_runner(server)
    return DhcpClient(limit(runner, get_setting("max_concurrency")), computer_name=server.computer_name)


def _windows_hosts(dhcp_server):
    from .models import WindowsServer

    ws = WindowsServer.objects.filter(dhcp_server=dhcp_server).first()
    return (ws.host, ws.computer_name) if ws else ()


def failover_secret_lookup(server):
    """
    A function returning the shared secret of one of the server's failover relationships, by name, for the push
    writer. The secret is read from the relationship's credential profile when it is needed and goes straight to the
    cmdlet. Both partners receive it, so the profile's allowed_hosts must cover both, as for any credentials.
    """
    from .models import FailoverRelationship

    def lookup(name):
        relationship = (
            FailoverRelationship.objects.filter(primary_server=server.dhcp_server, name__iexact=name)
            .select_related("partner_server")
            .first()
        )
        if relationship is None or not relationship.shared_secret_profile:
            return ""
        profile = relationship.shared_secret_profile
        partner_hosts = _windows_hosts(relationship.partner_server)
        if not partner_hosts:
            # Without the partner's host there is nothing to check the profile's allowed_hosts against.
            raise ValueError(f"failover {name}: the partner server has no Windows server in NetBox; add one")
        try:
            check_host_allowed(profile, server.host, server.computer_name, *partner_hosts)
            return get_credentials(profile).secret
        except CredentialError as exc:
            # The push reports ValueError as a failed change, with this message (which never holds the secret).
            raise ValueError(f"failover {name}: {exc}") from None

    return lookup
