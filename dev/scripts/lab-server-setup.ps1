<#
.SYNOPSIS
    Prepares a Windows Server VM as the lab DHCP server for `make test-lab` and `make lab-e2e`.

.DESCRIPTION
    Run once inside the VM, in an elevated Windows PowerShell or pwsh session. Safe to run again.

    It does four things:
      1. installs the DHCP Server role and its PowerShell module, creates the "DHCP Administrators" and
         "DHCP Users" groups and marks the post-install task as done (no Active Directory needed: the lab
         server never hands out real leases, so it does not have to be authorized in a domain);
      2. creates a local, non-admin account for the plugin (default nbwd-lab), member of
         "DHCP Administrators" (DHCP cmdlets) and "Remote Management Users" (WinRM), which is the least
         privilege the plugin needs;
      3. turns on WinRM with an HTTPS listener on 5986, using a self-signed certificate (the lab side sets
         WINDOWS_DHCP_LAB_INSECURE=1 to accept it); NTLM over HTTPS works out of the box, Basic stays off;
      4. opens TCP 5986 in Windows Firewall, optionally only from the given addresses.

    It creates no scopes: the lab tests create their own (198.18.0.0/15, 2001:db8:4e42::/64, nbwd-test-*) and
    remove them afterwards. A host-only or internal virtual network is enough.

.PARAMETER LabUser
    Local account the plugin connects with. Pass an empty string to skip creating it (for example to use a
    domain account you already put in "DHCP Administrators").

.PARAMETER ResetPassword
    Ask for a new password when the account already exists.

.PARAMETER AllowFrom
    Remote addresses or ranges allowed to reach WinRM over HTTPS, for example 192.168.56.0/24. Default: any.

.PARAMETER ExtraDnsNames
    More names or addresses to put in the certificate, besides the computer name and its FQDN.

.EXAMPLE
    Set-ExecutionPolicy -Scope Process Bypass
    .\lab-server-setup.ps1 -AllowFrom 192.168.56.0/24
#>
[CmdletBinding()]
param(
    [string]$LabUser = 'nbwd-lab',
    [switch]$ResetPassword,
    [string[]]$AllowFrom = @('Any'),
    [string[]]$ExtraDnsNames = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }

# -- checks ----------------------------------------------------------------------------------------------------------

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated (Run as administrator) PowerShell session.'
}
# Install-WindowsFeature only exists on Windows Server; the DHCP Server role is not available on client Windows.
if (-not (Get-Command Install-WindowsFeature -ErrorAction SilentlyContinue)) {
    throw 'Install-WindowsFeature is missing: this script needs Windows Server 2016 or later.'
}

# -- 1. DHCP Server role ---------------------------------------------------------------------------------------------

Write-Step 'DHCP Server role'
$feature = Get-WindowsFeature -Name DHCP
if (-not $feature.Installed) {
    $result = Install-WindowsFeature -Name DHCP -IncludeManagementTools
    if ($result.RestartNeeded -eq 'Yes') {
        Write-Warning 'The role asks for a restart. Restart the VM, then run this script again.'
        return
    }
} else {
    # The role can be present without the PowerShell module (RSAT-DHCP); the plugin needs the module.
    Install-WindowsFeature -Name RSAT-DHCP | Out-Null
}
Import-Module DhcpServer

# Creates the local "DHCP Administrators" and "DHCP Users" groups. The service reads them at start.
Add-DhcpServerSecurityGroup
Set-Service -Name DHCPServer -StartupType Automatic
Restart-Service -Name DHCPServer

# Tells Server Manager the post-install configuration is done, so it stops asking to authorize the server.
$roleKey = 'HKLM:\SOFTWARE\Microsoft\ServerManager\Roles\12'
if (Test-Path $roleKey) {
    Set-ItemProperty -Path $roleKey -Name ConfigurationState -Value 2
}

# The service only answers clients on interfaces with a static address. The tests never need real clients, so
# this is only a warning.
$static = Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Manual -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' }
if (-not $static) {
    Write-Warning 'No static IPv4 address: fine for the lab tests, but the server will not serve real clients.'
}

# -- 2. Account for the plugin ---------------------------------------------------------------------------------------

if ($LabUser) {
    Write-Step "Local account $LabUser"
    $existing = Get-LocalUser -Name $LabUser -ErrorAction SilentlyContinue
    if (-not $existing -or $ResetPassword) {
        $first = Read-Host -AsSecureString "Password for $LabUser"
        $second = Read-Host -AsSecureString 'Repeat the password'
        $plain = { param($s) [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
                [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)) }
        if ((& $plain $first) -cne (& $plain $second)) { throw 'The passwords do not match.' }
        if ($existing) {
            Set-LocalUser -Name $LabUser -Password $first
        } else {
            New-LocalUser -Name $LabUser -Password $first -PasswordNeverExpires `
                -Description 'netbox-windows-dhcp-plugin lab tests' | Out-Null
        }
    }
    foreach ($group in 'DHCP Administrators', 'Remote Management Users') {
        $member = Get-LocalGroupMember -Group $group -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "*\$LabUser" }
        if (-not $member) { Add-LocalGroupMember -Group $group -Member $LabUser }
    }
}

# -- 3. WinRM over HTTPS ---------------------------------------------------------------------------------------------

Write-Step 'WinRM over HTTPS'
# SkipNetworkProfileCheck: a host-only adapter is often classed as a Public network, where this would fail.
Enable-PSRemoting -SkipNetworkProfileCheck -Force | Out-Null
Set-Service -Name WinRM -StartupType Automatic

$fqdn = [Net.Dns]::GetHostEntry($env:COMPUTERNAME).HostName
$dnsNames = @($env:COMPUTERNAME, $fqdn) + $ExtraDnsNames | Where-Object { $_ } | Select-Object -Unique

# Reuse the certificate from an earlier run when it still covers the same names.
$subject = "CN=$($env:COMPUTERNAME)"
$cert = Get-ChildItem Cert:\LocalMachine\My |
    Where-Object {
        $_.Subject -eq $subject -and $_.FriendlyName -eq 'nbwd-lab WinRM' -and $_.NotAfter -gt (Get-Date).AddDays(30) -and
        -not (Compare-Object @($_.DnsNameList.Unicode) @($dnsNames))
    } | Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -DnsName $dnsNames -CertStoreLocation Cert:\LocalMachine\My `
        -FriendlyName 'nbwd-lab WinRM' -NotAfter (Get-Date).AddYears(2)
}

Get-ChildItem WSMan:\localhost\Listener |
    Where-Object { $_.Keys -contains 'Transport=HTTPS' } |
    Remove-Item -Recurse -Force
New-Item -Path WSMan:\localhost\Listener -Transport HTTPS -Address * -CertificateThumbPrint $cert.Thumbprint `
    -Force | Out-Null

# The plugin authenticates with NTLM (Negotiate). Basic sends the password itself, keep it off.
Set-Item WSMan:\localhost\Service\Auth\Negotiate -Value $true
Set-Item WSMan:\localhost\Service\Auth\Basic -Value $false
Set-Item WSMan:\localhost\Service\AllowUnencrypted -Value $false

# -- 4. Firewall -----------------------------------------------------------------------------------------------------

Write-Step 'Firewall rule for TCP 5986'
$ruleName = 'nbwd-lab WinRM HTTPS'
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort 5986 -Action Allow `
    -Profile Any -RemoteAddress $AllowFrom | Out-Null

# -- summary ---------------------------------------------------------------------------------------------------------

Write-Step 'Self-check'
$listener = winrm enumerate winrm/config/listener | Out-String
if ($listener -notmatch 'Transport = HTTPS') { throw 'No HTTPS listener after setup.' }
Get-DhcpServerSetting | Out-Null

$addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress

Write-Host ''
Write-Host 'Done. On the NetBox side, put these in dev/env/netbox.env (never commit it):' -ForegroundColor Green
Write-Host ''
Write-Host "    WINDOWS_DHCP_LAB_HOST=<one of: $(($dnsNames + $addresses) -join ', ')>"
if ($LabUser) {
    Write-Host "    WINDOWS_DHCP_USERNAME=$($env:COMPUTERNAME)\$LabUser"
} else {
    Write-Host '    WINDOWS_DHCP_USERNAME=<DOMAIN\user in DHCP Administrators and Remote Management Users>'
}
Write-Host '    WINDOWS_DHCP_PASSWORD=<the password you just typed>'
Write-Host '    WINDOWS_DHCP_LAB_INSECURE=1'
Write-Host ''
Write-Host "Certificate thumbprint: $($cert.Thumbprint) (self-signed, valid until $($cert.NotAfter.ToString('yyyy-MM-dd')))"
Write-Host 'Then: make test-lab, and make dev followed by make lab-e2e.'
