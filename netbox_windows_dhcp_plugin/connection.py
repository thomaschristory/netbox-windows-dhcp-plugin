"""Build a Windows DHCP client for a WindowsServer, resolving its credential profile."""

from .choices import FailoverAuthChoices, TransportChoices, WinRMAuthChoices
from .conf import (
    CredentialError,
    check_host_allowed,
    connection_pin_errors,
    get_credentials,
    get_setting,
    host_allowed,
    insecure_credssp_allowed,
    max_concurrency,
    profile_ssh_host_keys,
)
from .windows import DhcpClient, limit
from .windows.psscript import WindowsLayerError
from .windows.runner import RunnerError


class FailoverLogonError(WindowsLayerError):
    """
    The failover client's logon or connection failed, or one of its commands timed out (issue #141). Not a
    RunnerError on purpose: a push stops at a RunnerError (the server is unreachable), while this only concerns the
    failover writes, so the push fails those (and what depends on them) and goes on with the rest, which the
    server's own client sends. A failover write that timed out after it was sent may still finish on Windows; the
    next push reads Windows again and reconciles.
    """


class _FailoverRunner:
    """Name the failover logon in its errors, and keep them from stopping the whole push."""

    def __init__(self, runner, auth):
        self.runner, self.auth = runner, auth

    def run(self, script, params=None):
        try:
            return self.runner.run(script, params)
        except RunnerError as exc:  # Stopped is not a RunnerError: a stop passes through as it is
            raise FailoverLogonError(f"failover authentication ({self.auth}): {exc}") from exc

    def close(self):
        close = getattr(self.runner, "close", None)
        if callable(close):
            close()


def kerberos_realm(server):
    """The server's realm as the connection uses it (and the profile's kerberos_realms check reads it)."""
    return (server.kerberos_realm or "").strip().upper()


def check_connection_allowed(server):
    """
    Refuse to send credentials where they could leak, even if the record skipped form validation (for example a
    host changed in the database, or a profile's allowed_hosts tightened after the server was saved).
    """
    # A value outside the choices (a record written straight to the database, "KERBEROS" or "Basic") would slip past
    # the exact comparisons below while the transport reads it its own way (WinRM lower-cases the method, and anything
    # but "ssh" is WinRM), so it is refused before anything else.
    for field, choices in (
        ("transport", TransportChoices),
        ("winrm_auth", WinRMAuthChoices),
        ("failover_auth", FailoverAuthChoices),
    ):
        if getattr(server, field) not in choices.values():
            raise CredentialError(f"unknown {field} {getattr(server, field)!r}")
    # computer_name counts too: with delegation the first host passes the credentials on to it.
    check_host_allowed(server.credential_profile, server.host, server.computer_name)
    # The profile's transports, ports, Kerberos realms and SSH host keys (issue #103). check_host_allowed has already
    # refused an unknown profile.
    errors = connection_pin_errors(
        server.credential_profile,
        get_setting("credentials")[server.credential_profile],
        transport=server.transport,
        port=server.effective_port,
        winrm_auth=server.winrm_auth,
        kerberos_realm=kerberos_realm(server),
        ssh_host_key=server.ssh_host_key,
    )
    if errors:
        raise CredentialError("; ".join(errors.values()))
    if (
        server.transport == TransportChoices.WINRM
        and server.winrm_auth == WinRMAuthChoices.BASIC
        and not (server.winrm_https and server.verify_tls)
    ):
        raise CredentialError("Basic authentication needs HTTPS with certificate verification")
    credssp = server.winrm_auth == WinRMAuthChoices.CREDSSP or server.failover_auth == FailoverAuthChoices.CREDSSP
    if (
        server.transport == TransportChoices.WINRM
        and credssp
        and not (server.winrm_https and server.verify_tls)
        and not insecure_credssp_allowed(server.credential_profile)
    ):
        raise CredentialError(
            "CredSSP hands the password to the server, so it needs HTTPS with certificate verification "
            "(or allow_insecure_credssp on the credential profile, for labs)"
        )


def build_runner(server, auth=""):
    """The server's runner; ``auth`` overrides its WinRM authentication (the failover client, issue #141)."""
    check_connection_allowed(server)
    creds = get_credentials(server.credential_profile)
    timeout = get_setting("command_timeout")
    if server.transport == TransportChoices.SSH:
        from .windows.ssh import SSHConfig, SSHRunner

        profile = get_setting("credentials")[server.credential_profile]
        trusted = profile_ssh_host_keys(server.credential_profile, profile)
        return SSHRunner(
            SSHConfig(
                host=server.host,
                username=creds.username,
                password=creds.password,
                private_key=creds.private_key,
                port=server.effective_port,
                host_key=server.ssh_host_key,
                # With the profile's ssh_host_keys, only those keys are trusted: a server without its own key no
                # longer falls back to the worker's known_hosts (issue #103).
                trusted_host_keys=None if trusted is None else tuple(trusted),
                # Never skip the host key check: a machine in the middle would receive the password.
                insecure=False,
                command_timeout=timeout,
            )
        )
    from .windows.winrm import WinRMConfig, WinRMRunner

    username = creds.username
    realm = kerberos_realm(server)
    if (
        auth == WinRMAuthChoices.CREDSSP
        and server.winrm_auth == WinRMAuthChoices.KERBEROS
        and realm
        and not any(c in username for c in "\\@")
    ):
        # A Kerberos server's profile may hold a bare user name (the realm comes from the server); CredSSP needs
        # the domain.
        username = f"{username}@{realm}"
    return WinRMRunner(
        WinRMConfig(
            host=server.host,
            username=username,
            password=creds.password,
            port=server.effective_port,
            https=server.winrm_https,
            cert_validation=server.verify_tls,
            auth=auth or server.winrm_auth,
            kerberos_realm=realm,
            kerberos_spn=server.kerberos_spn,
            timeout=timeout,
        )
    )


def build_client(server, runner=None):
    """Return a DhcpClient for the server. Pass `runner` to substitute a fake in tests."""
    check_connection_allowed(server)
    runner = runner if runner is not None else build_runner(server)
    # The real cap on commands in flight: an import or push reads up to max_concurrency scopes at the same time.
    return DhcpClient(limit(runner, max_concurrency()), computer_name=server.computer_name)


def build_failover_client(server, runner=None):
    """
    A second client for the calls that make the server reach its failover partner as the caller (creating, changing
    and deleting a relationship, its scopes, replication), when the server sets a failover authentication other than
    its own (issue #141): Kerberos and NTLM cannot carry that hop, CredSSP can. None when the server's own client
    serves. Pass `runner` to substitute a fake in tests.
    """
    # Checked first, so a stored value outside the choices is refused here too rather than read as "no second client".
    check_connection_allowed(server)
    auth = server.separate_failover_auth
    if not auth:
        return None
    runner = runner if runner is not None else build_runner(server, auth=auth)
    # Failover writes are few and one at a time; the same cap keeps the server's total in check.
    return DhcpClient(limit(_FailoverRunner(runner, auth), max_concurrency()), computer_name=server.computer_name)


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
            # The secret travels over this server's own connection, so the secret profile's transports, ports,
            # Kerberos realms and SSH host keys apply to that connection too (issue #103). Partners stay
            # allowed_hosts only.
            errors = connection_pin_errors(
                profile,
                get_setting("credentials")[profile],
                transport=server.transport,
                port=server.effective_port,
                winrm_auth=server.winrm_auth,
                kerberos_realm=kerberos_realm(server),
                ssh_host_key=server.ssh_host_key,
            )
            if errors:
                raise CredentialError("; ".join(errors.values()))
            # Only the secret is used here, so only an inline None for the secret is an error.
            return get_credentials(profile, needed=("secret",)).secret
        except CredentialError as exc:
            # The push reports ValueError as a failed change, with this message (which never holds the secret).
            raise ValueError(f"failover {name}: {exc}") from None

    return lookup
