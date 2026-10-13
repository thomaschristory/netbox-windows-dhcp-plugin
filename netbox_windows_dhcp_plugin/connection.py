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
                insecure=not server.verify_tls,
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
