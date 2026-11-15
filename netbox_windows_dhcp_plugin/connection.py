"""Build a Windows DHCP client for a WindowsServer, resolving its credential profile."""

from .choices import TransportChoices, WinRMAuthChoices
from .conf import (
    CredentialError,
    check_host_allowed,
    get_credentials,
    get_setting,
    host_allowed,
    insecure_credssp_allowed,
)
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
    if (
        server.transport == TransportChoices.WINRM
        and server.winrm_auth == WinRMAuthChoices.CREDSSP
        and not (server.winrm_https and server.verify_tls)
        and not insecure_credssp_allowed(server.credential_profile)
    ):
        raise CredentialError(
            "CredSSP hands the password to the server, so it needs HTTPS with certificate verification "
            "(or allow_insecure_credssp on the credential profile, for labs)"
        )


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
    # The limit is never reached today: a job sends its commands one after another.
    return DhcpClient(limit(runner, get_setting("max_concurrency")), computer_name=server.computer_name)


def failover_partner_check(server):
    """
    A function for the push writer that refuses to create a failover relationship (by name) whose partner is outside
    the allowed_hosts of the server's own credential profile. Add-DhcpServerv4Failover makes the server talk to the
    partner as the calling account (with CredSSP, holding its password), so the partner is one more host the
    profile's credentials reach, like computer_name. Only creation names a partner chosen in NetBox: changes and
    deletes act on what Windows already has, and stay possible.
    """

    def check(name, partner):
        # `partner` is the host the writer is about to send (the partner's DHCP host, not a management host in
        # front of it). It is checked as given, not looked up again by name: the relationship may have been
        # renamed, repointed or deleted in NetBox since the push planned this change.
        profile = get_setting("credentials").get(server.credential_profile)
        if profile is None:
            # No profile: the connection itself is refused (or is a test's fake), there is nothing to protect.
            return
        try:
            allowed = host_allowed(server.credential_profile, profile, partner)
        except CredentialError as exc:
            raise ValueError(f"failover {name}: {exc}") from None
        if not allowed:
            # The push reports ValueError as a failed change, with this message.
            raise ValueError(
                f"failover {name}: credential profile {server.credential_profile!r} may not be used with the "
                f"partner host {partner!r}; add it to the profile's allowed_hosts"
            )

    return check


def failover_secret_lookup(server):
    """
    A function returning the shared secret of one of the server's failover relationships, by name, for the push
    writer. The secret is read from the relationship's credential profile when it is needed and goes straight to the
    cmdlet. Both partners receive it, so the profile's allowed_hosts must cover both, as for any credentials.
    """
    from .models import FailoverRelationship

    def lookup(name):
        relationship = (
            FailoverRelationship.objects.filter(primary_server=server, name__iexact=name)
            .select_related("partner_server")
            .first()
        )
        if relationship is None or not relationship.shared_secret_profile:
            return ""
        profile = relationship.shared_secret_profile
        partner = relationship.partner_server
        partner_hosts = (partner.host, partner.computer_name)
        try:
            check_host_allowed(profile, server.host, server.computer_name, *partner_hosts)
            # Only the secret is used here, so only an inline None for the secret is an error.
            return get_credentials(profile, needed=("secret",)).secret
        except CredentialError as exc:
            # The push reports ValueError as a failed change, with this message (which never holds the secret).
            raise ValueError(f"failover {name}: {exc}") from None

    return lookup
