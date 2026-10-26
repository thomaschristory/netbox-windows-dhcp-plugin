# Preparing Windows

The plugin runs PowerShell on the DHCP server, with the `DhcpServer` module that comes with the DHCP Server role.
Each DHCP server needs:

- Windows Server 2016 or later with the DHCP Server role and its management tools.
- An account the plugin connects with, allowed to manage DHCP and to log in remotely.
- WinRM (recommended: HTTPS on port 5986) or the OpenSSH server.

!!! tip "Lab server"
    For a test VM, `dev/scripts/lab-server-setup.ps1` in the repository does all of this in one go (role, a local
    account, WinRM over HTTPS with a self-signed certificate, firewall rule). See [Development](development.md#lab-tests).

## The account

The least privilege the plugin needs is:

- membership of the local **DHCP Administrators** group on each DHCP server (add the domain account, or a domain
  group holding it, on each server), to read and change the DHCP configuration; and
- membership of **Remote Management Users** (WinRM) on each server, or permission to log in over SSH.

A domain account (for example `CORP\svc-netbox-dhcp`) is easiest with several servers. It does not need to be a
local or domain administrator.

With failover, the account needs DHCP Administrators on both partners. After a push, the plugin replicates from the
primary (`Invoke-DhcpServerv4FailoverReplication`), and that cmdlet contacts the partner from inside the remote
session. Whether this needs delegation (the "double hop" described [below](#managing-a-dhcp-server-through-another-host))
has not been checked on a real pair of servers yet; see [Limitations](limitations.md).

If the DHCP Administrators group does not exist yet, create it on the server and restart the service:

```powershell
Add-DhcpServerSecurityGroup
Restart-Service DHCPServer
```

## WinRM over HTTPS (recommended)

WinRM over HTTPS with NTLM is the default the plugin expects (port 5986, certificate checked).

1. Get a certificate for the server's name from your CA, with the "Server Authentication" usage, into
   `Cert:\LocalMachine\My`. The name must match the `host` you will enter in NetBox.
2. Create the listener and open the port:

    ```powershell
    Enable-PSRemoting -Force
    $thumbprint = (Get-ChildItem Cert:\LocalMachine\My | Where-Object Subject -eq "CN=dhcp01.corp.example").Thumbprint
    New-Item -Path WSMan:\localhost\Listener -Transport HTTPS -Address * -CertificateThumbPrint $thumbprint -Force
    New-NetFirewallRule -DisplayName "WinRM HTTPS" -Direction Inbound -Protocol TCP -LocalPort 5986 -Action Allow
    ```

3. Check from another Windows machine:

    ```powershell
    Test-WSMan -ComputerName dhcp01.corp.example -UseSSL -Authentication Negotiate -Credential CORP\svc-netbox-dhcp
    ```

NetBox checks the certificate against the CA bundle of the Python `requests` library. For an internal CA, point
`REQUESTS_CA_BUNDLE` at a PEM file that includes it, in the environment of both the NetBox web service and the
worker.

Turning off **Verify host** on the Windows server in NetBox skips the certificate check. Only do that in a lab: anyone
in the network path could then read the traffic.

## WinRM over HTTP

Over HTTP (port 5985), NTLM, Kerberos and CredSSP still encrypt every message, so the server can keep
`AllowUnencrypted = false`. Untick **WinRM over HTTPS** on the Windows server in NetBox. HTTPS is still preferred:
NTLM over HTTP does not prove the identity of the server.

## Authentication methods

| Method | When to use | Notes |
|---|---|---|
| NTLM (default) | Domain or local accounts | Works out of the box with WinRM. User name as `DOMAIN\user`, or `user` for a local account |
| Kerberos | Domain accounts, NTLM disabled | Needs the `[kerberos]` extra, GSSAPI libraries and `krb5.conf` where the worker runs, a realm on the Windows server in NetBox, and the server's real DNS name as `host` |
| CredSSP | Failover relationships | Needed to create or change a [failover relationship](failover.md#connecting-to-the-servers) or replicate it: the cmdlets make the server call its partner as you. Enable it on each DHCP server with `Enable-WSManCredSSP -Role Server -Force`. The password is handed to the DHCP server, so use an account that can only manage DHCP |
| Basic | Local accounts only, when NTLM is not possible | Sends the password itself, so the plugin only allows it over HTTPS with certificate verification. Enable it on the server with `Set-Item WSMan:\localhost\Service\Auth\Basic $true` |

For Kerberos, the plugin gets a ticket with the profile's user name and password; the user name may be given as
`DOMAIN\user` or `user@REALM`, and the realm entered on the Windows server in NetBox is used. The service principal
defaults to `WSMAN/<host>`; set **Kerberos SPN** when the server registers another one.

## OpenSSH

The plugin can also connect over SSH to the Windows OpenSSH server and runs `powershell.exe` there.

1. Install and start the server:

    ```powershell
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
    Start-Service sshd
    Set-Service -Name sshd -StartupType Automatic
    ```

2. Use a password or a private key in the [credential profile](configuration.md#credential-profiles).
3. Give NetBox the server's host key, in one of two ways:
    - paste it into **SSH host key** on the Windows server in NetBox, in `authorized_keys` form (for example
      `ssh-ed25519 AAAA...`). Read it on the server from `C:\ProgramData\ssh\ssh_host_ed25519_key.pub`;
    - or add it to `~/.ssh/known_hosts` of the user running the NetBox worker (and the web service), for example with
      `ssh-keyscan -t ed25519 dhcp01.corp.example`.

The plugin never accepts an unknown host key, and **Verify host** cannot be turned off for SSH.

## Managing a DHCP server through another host

**Remote DHCP server** (`computer_name`) on the Windows server in NetBox runs the cmdlets against another server
(`-ComputerName`) through the host you connect to. Your credentials then have to travel one hop further, which
Windows only allows with CredSSP (choose it as the WinRM authentication) or Kerberos constrained delegation. Connecting straight to each DHCP server is
simpler and recommended. When you do use it, the credential profile's `allowed_hosts` must cover both names.

## Checking the account by hand

On the DHCP server, as the plugin's account:

```powershell
Get-DhcpServerv4Scope
Get-DhcpServerSetting
```

Both must work without an "access denied" error. Then use **Test connection** in NetBox (see
[Windows servers](servers.md#test-connection)).
