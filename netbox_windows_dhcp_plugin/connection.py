"""Build a Windows DHCP client for a WindowsServer, resolving its credential profile."""

from .choices import TransportChoices
from .conf import get_credentials, get_setting
from .windows import DhcpClient, limit


def build_runner(server):
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
    runner = runner if runner is not None else build_runner(server)
    return DhcpClient(limit(runner, get_setting("max_concurrency")), computer_name=server.computer_name)
