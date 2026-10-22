# Stub DhcpServer cmdlets for script tests run under pwsh. Parameter names
# and types follow the documented cmdlet signatures. Like the real CDXML
# cmdlets, failures are written as non-terminating errors, so the scripts
# only stop on them because of the -ErrorAction default they set.
#
# Ported from terraform-provider-windowsddi internal/dhcp/testdata/stubs.ps1 and
# extended with the cmdlets the plugin adds (classes, failover, superscopes, filters,
# option definitions, policies, DHCPv6). Parameter lists for the added cmdlets follow
# the documented signatures as far as known; they are not verified against Windows unless
# a comment cites a lab finding (issue number).
#
# Used by tests/windows/test_pwsh.py, which loads this file as an in-memory
# module (New-Module ... | Import-Module) before each script. Every stub:
#   1. records its call (cmdlet name + bound parameters) via Record, and
#   2. reads or updates the in-memory state below.
# State lives in globals and is lost when the pwsh process exits; tests
# rebuild it per run through a "seed" script.

# In-memory DHCP server state. Scopes and Reservations are keyed by IP string.
$global:Scopes = [ordered]@{}
$global:Reservations = [ordered]@{}
$global:Exclusions = [System.Collections.ArrayList]::new()
$global:Options = [System.Collections.ArrayList]::new()
$global:Leases = [System.Collections.ArrayList]::new()

# Common parameters (added by [CmdletBinding()]) that Record leaves out, so
# tests only see the DHCP-specific parameters a script passed.
$commonParams = 'ErrorAction', 'WarningAction', 'InformationAction', 'Verbose', 'Debug', 'ErrorVariable',
    'WarningVariable', 'InformationVariable', 'OutVariable', 'OutBuffer', 'PipelineVariable', 'ProgressAction'

# Record appends one JSON line { cmd = <name>; <param> = <value>; ... } to the
# file named by $env:WD_CALLS. Values are normalised so tests can compare them
# easily: TimeSpan -> total seconds, switch -> bool, IP -> string, array ->
# array of strings.
function Record($name, $bound) {
    if ($global:WdNoRecord) { return }  # set while the test seed runs
    $h = [ordered]@{ cmd = $name }
    foreach ($k in $bound.Keys) {
        if ($commonParams -contains $k) { continue }
        $v = $bound[$k]
        if ($v -is [TimeSpan]) { $v = $v.TotalSeconds }
        elseif ($v -is [System.Management.Automation.SwitchParameter]) { $v = $v.IsPresent }
        elseif ($v -is [ipaddress]) { $v = $v.ToString() }
        elseif ($v -is [array]) { $v = @($v | ForEach-Object { "$_" }) }
        $h[$k] = $v
    }
    Add-Content -LiteralPath $env:WD_CALLS -Value (ConvertTo-Json -InputObject $h -Compress -Depth 4) -Encoding utf8
}

# The DHCP server runs the scripts in Windows PowerShell 5.1, which writes a value holding AutomationNull (what a
# variable keeps after "$out = <a command with no output>") as {} in JSON, where pwsh 7 writes null. A getter for a
# missing object then looked like an empty object (issue #15). This wrapper reproduces the 5.1 behaviour for the
# envelope's top-level values, so the stub tests see what a real server prints. AutomationNull is told apart from a
# real $null by @(...).Count: 0 for AutomationNull, 1 for $null (and an empty array is not -eq $null).
function ConvertTo-Json {
    [CmdletBinding()]
    param([Parameter(Mandatory, Position = 0, ValueFromPipeline)][AllowNull()][object]$InputObject,
        [int]$Depth = 2, [switch]$Compress)
    process {
        if ($InputObject -is [System.Collections.IDictionary]) {
            foreach ($k in @($InputObject.Keys)) {
                if ($null -eq $InputObject[$k] -and @($InputObject[$k]).Count -eq 0) { $InputObject[$k] = [pscustomobject]@{} }
            }
        }
        Microsoft.PowerShell.Utility\ConvertTo-Json -InputObject $InputObject -Depth $Depth -Compress:$Compress
    }
}

# Write-InvalidArgument emits the non-terminating InvalidArgument error the real cmdlets give for a parameter
# combination they refuse.
function Write-InvalidArgument($cmdlet, $message) {
    $er = [System.Management.Automation.ErrorRecord]::new(
        [ArgumentException]::new($message), 'InvalidArgument', 'InvalidArgument', $null)
    $cmdlet.WriteError($er)
}

# Write-DhcpError emits a non-terminating error shaped like the real cmdlets'
# ones: FullyQualifiedErrorId "DHCP <code>" and the given category (e.g.
# ObjectNotFound), so psscript's not-found detection is exercised.
function Write-DhcpError($cmdlet, $code, $category, $message) {
    $er = [System.Management.Automation.ErrorRecord]::new(
        [Exception]::new($message), "DHCP $code", $category, $null)
    $cmdlet.WriteError($er)
}

# New-Scope builds a scope object with the same property types as the real
# CIM object (IP addresses, TimeSpan). Like the real server, it reports the
# input state 'InActive' back as 'Inactive'.
function New-Scope($p) {
    [pscustomobject]@{
        ScopeId       = [ipaddress]$p.ScopeId
        SubnetMask    = [ipaddress]$p.SubnetMask
        Name          = $p.Name
        State         = if ($p.State -eq 'InActive') { 'Inactive' } else { $p.State }
        StartRange    = [ipaddress]$p.StartRange
        EndRange      = [ipaddress]$p.EndRange
        LeaseDuration = [TimeSpan]$p.LeaseDuration
        Description   = $p.Description
        Type          = $p.Type
    }
}

# ---- Scopes ----

# No -ScopeId: list all. Unknown ID: DHCP 20005 ObjectNotFound error.
function Get-DhcpServerv4Scope {
    [CmdletBinding()]
    param([string]$ComputerName, [Parameter(Position = 0)][ipaddress[]]$ScopeId)
    Record 'Get-DhcpServerv4Scope' $PSBoundParameters
    if (-not $ScopeId) { return $global:Scopes.Values }
    foreach ($id in $ScopeId) {
        if ($global:Scopes.Contains("$id")) { $global:Scopes["$id"] }
        else { Write-DhcpError $PSCmdlet 20005 ObjectNotFound "Failed to get information for scope $id on DHCP server STUB." }
    }
}

# Derives the scope ID as StartRange AND SubnetMask, as the real server does.
# A duplicate subnet gives a DHCP 20052 error.
function Add-DhcpServerv4Scope {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName,
        [Parameter(Mandatory)][ipaddress]$StartRange, [Parameter(Mandatory)][ipaddress]$EndRange,
        [Parameter(Mandatory)][string]$Name, [string]$Description,
        [ValidateSet('Active', 'InActive')][string]$State = 'Active',
        [TimeSpan]$LeaseDuration = '8.00:00:00', [Parameter(Mandatory)][ipaddress]$SubnetMask,
        [ValidateSet('Dhcp', 'Bootp', 'Both')][string]$Type = 'Dhcp', [switch]$PassThru,
        [uint16]$Delay, [uint32]$MaxBootpClients, [bool]$ActivatePolicies, [string]$SuperscopeName)
    Record 'Add-DhcpServerv4Scope' $PSBoundParameters
    $mask = $SubnetMask.GetAddressBytes(); $start = $StartRange.GetAddressBytes()
    $net = [ipaddress][byte[]](0..3 | ForEach-Object { $start[$_] -band $mask[$_] })
    if ($global:Scopes.Contains("$net")) {
        Write-DhcpError $PSCmdlet 20052 InvalidArgument "Failed to add scope $net. The specified subnet already exists."
        return
    }
    $s = New-Scope @{ ScopeId = $net; SubnetMask = $SubnetMask; Name = $Name; State = $State; StartRange = $StartRange
        EndRange = $EndRange; LeaseDuration = $LeaseDuration; Description = $Description; Type = $Type }
    $global:Scopes["$net"] = $s
    if ($PassThru) { $s }
}

# Replaces the scope with the given values (keeps the subnet mask, which Set
# cannot change). Unknown ID: DHCP 20005 ObjectNotFound error.
function Set-DhcpServerv4Scope {
    [CmdletBinding(SupportsShouldProcess, DefaultParameterSetName = 'WithoutRange')]
    param([Parameter(Mandatory, Position = 0)][ipaddress]$ScopeId, [string]$ComputerName,
        [string]$Name, [string]$Description,
        [ValidateSet('Active', 'InActive')][string]$State, [TimeSpan]$LeaseDuration,
        [ValidateSet('Dhcp', 'Bootp', 'Both')][string]$Type,
        [Parameter(Mandatory, ParameterSetName = 'WithRange')][ipaddress]$StartRange,
        [Parameter(Mandatory, ParameterSetName = 'WithRange')][ipaddress]$EndRange, [switch]$PassThru,
        [uint16]$Delay, [uint32]$MaxBootpClients, [bool]$ActivatePolicies, [string]$SuperscopeName)
    Record 'Set-DhcpServerv4Scope' $PSBoundParameters
    if (-not $global:Scopes.Contains("$ScopeId")) {
        Write-DhcpError $PSCmdlet 20005 ObjectNotFound "Failed to set scope $ScopeId."
        return
    }
    $old = $global:Scopes["$ScopeId"]
    if (-not $PSBoundParameters.ContainsKey('LeaseDuration')) { $LeaseDuration = $old.LeaseDuration }
    $global:Scopes["$ScopeId"] = New-Scope @{ ScopeId = $ScopeId; SubnetMask = $old.SubnetMask; Name = $Name; State = $State
        StartRange = $StartRange; EndRange = $EndRange; LeaseDuration = $LeaseDuration; Description = $Description; Type = $Type }
}

# Removes silently; does not model the "scope has active leases" failure.
function Remove-DhcpServerv4Scope {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory, Position = 0)][ipaddress[]]$ScopeId, [string]$ComputerName, [switch]$Force, [switch]$Passthru)
    Record 'Remove-DhcpServerv4Scope' $PSBoundParameters
    foreach ($id in $ScopeId) { $global:Scopes.Remove("$id") }
}

# ---- Reservations ----

# Lists a scope's reservations. Unknown scope: DHCP 20005 ObjectNotFound.
function Get-DhcpServerv4Reservation {
    [CmdletBinding()]
    param([string]$ComputerName, [Parameter(Position = 0)][ipaddress]$ScopeId)
    Record 'Get-DhcpServerv4Reservation' $PSBoundParameters
    if (-not $global:Scopes.Contains("$ScopeId")) {
        Write-DhcpError $PSCmdlet 20005 ObjectNotFound "Failed to enumerate reservations in scope $ScopeId."
        return
    }
    $global:Reservations.Values | Where-Object { "$($_.ScopeId)" -eq "$ScopeId" }
}

# Like the real server: an empty Name defaults to the IP address, and the MAC
# is stored lowercase with dashes.
function Add-DhcpServerv4Reservation {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory, Position = 0)][ipaddress]$ScopeId,
        [Parameter(Mandatory, Position = 1)][ipaddress]$IPAddress, [Parameter(Mandatory, Position = 2)][string]$ClientId,
        [string]$Name, [string]$Description, [ValidateSet('Dhcp', 'Bootp', 'Both')][string]$Type = 'Both', [switch]$PassThru)
    Record 'Add-DhcpServerv4Reservation' $PSBoundParameters
    if (-not $Name) { $Name = "$IPAddress" }
    $global:Reservations["$IPAddress"] = [pscustomobject]@{ IPAddress = $IPAddress; ScopeId = $ScopeId
        ClientId = $ClientId.ToLower().Replace(':', '-'); Name = $Name; Type = $Type; Description = $Description }
}

# Keyed by IP only. Only parameters actually passed change; an explicitly
# passed empty Description clears it.
function Set-DhcpServerv4Reservation {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory, Position = 0)][ipaddress]$IPAddress, [string]$ClientId,
        [string]$Name, [string]$Description, [ValidateSet('Dhcp', 'Bootp', 'Both')][string]$Type, [switch]$PassThru)
    Record 'Set-DhcpServerv4Reservation' $PSBoundParameters
    $r = $global:Reservations["$IPAddress"]
    if ($ClientId) { $r.ClientId = $ClientId }
    if ($Name) { $r.Name = $Name }
    if ($PSBoundParameters.ContainsKey('Description')) { $r.Description = $Description }
    if ($Type) { $r.Type = $Type }
}

function Remove-DhcpServerv4Reservation {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][ipaddress[]]$IPAddress, [switch]$PassThru)
    Record 'Remove-DhcpServerv4Reservation' $PSBoundParameters
    foreach ($ip in $IPAddress) { $global:Reservations.Remove("$ip") }
}

# ---- Exclusion ranges ----

# Lists the exclusion ranges of the first scope given.
function Get-DhcpServerv4ExclusionRange {
    [CmdletBinding()]
    param([string]$ComputerName, [Parameter(Position = 0)][ipaddress[]]$ScopeId)
    Record 'Get-DhcpServerv4ExclusionRange' $PSBoundParameters
    $global:Exclusions | Where-Object { "$($_.ScopeId)" -eq "$($ScopeId[0])" }
}

function Add-DhcpServerv4ExclusionRange {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory, Position = 0)][ipaddress]$ScopeId,
        [Parameter(Mandatory, Position = 1)][ipaddress]$StartRange, [Parameter(Mandatory, Position = 2)][ipaddress]$EndRange, [switch]$PassThru)
    Record 'Add-DhcpServerv4ExclusionRange' $PSBoundParameters
    [void]$global:Exclusions.Add([pscustomobject]@{ ScopeId = $ScopeId; StartRange = $StartRange; EndRange = $EndRange })
}

# Only records the call; tests check the parameters it received.
function Remove-DhcpServerv4ExclusionRange {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory, Position = 0)][ipaddress]$ScopeId,
        [Parameter(Position = 1)][ipaddress]$StartRange, [Parameter(Position = 2)][ipaddress]$EndRange, [switch]$Passthru)
    Record 'Remove-DhcpServerv4ExclusionRange' $PSBoundParameters
}

# ---- Option values ----
# Each stored option has a Level string: "" for server, the scope ID, or the
# reserved IP. Get returns every option at the requested level. A reservation
# is named by its IP alone: Get tolerates a ScopeId next to ReservedIP (seen on
# Windows Server 2025) and ignores it, while Set and Remove refuse the pair with
# InvalidArgument, as the real cmdlets do (issue #20).
function Get-DhcpServerv4OptionValue {
    [CmdletBinding()]
    param([string]$VendorClass, [string]$ComputerName, [Parameter(Position = 0)][ipaddress]$ScopeId,
        [ipaddress]$ReservedIP, [Parameter(Position = 1)][uint32[]]$OptionId, [string]$UserClass, [switch]$All,
        [string]$PolicyName)
    Record 'Get-DhcpServerv4OptionValue' $PSBoundParameters
    $level = if ($ReservedIP) { "$ReservedIP" } else { "$ScopeId" }
    $global:Options | Where-Object { "$($_.Level)" -eq $level }
}

# Appends an option (does not replace an existing one; tests never set the
# same option twice). Name is always 'Router'.
function Set-DhcpServerv4OptionValue {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Position = 0)][ipaddress]$ScopeId, [Parameter(Mandatory, Position = 2)][string[]]$Value,
        [Parameter(Mandatory, Position = 1)][uint32]$OptionId, [switch]$PassThru, [switch]$Force,
        [ipaddress]$ReservedIP, [string]$UserClass, [string]$ComputerName, [string]$VendorClass, [string]$PolicyName)
    Record 'Set-DhcpServerv4OptionValue' $PSBoundParameters
    if ($ScopeId -and $ReservedIP) {
        Write-InvalidArgument $PSCmdlet 'ScopeId and ReservedIP cannot be specified together.'
        return
    }
    [void]$global:Options.Add([pscustomobject]@{ Level = "$ScopeId$ReservedIP"; OptionId = $OptionId; Name = 'Router'
        Value = $Value; VendorClass = $VendorClass; UserClass = $UserClass })
}

# Records the call and refuses ScopeId with ReservedIP; tests check the parameters it received.
function Remove-DhcpServerv4OptionValue {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$VendorClass, [string]$ComputerName, [Parameter(Mandatory, Position = 0)][uint32[]]$OptionId,
        [string]$UserClass, [Parameter(Position = 1)][ipaddress]$ScopeId, [ipaddress]$ReservedIP, [switch]$PassThru,
        [string]$PolicyName)
    Record 'Remove-DhcpServerv4OptionValue' $PSBoundParameters
    if ($ScopeId -and $ReservedIP) { Write-InvalidArgument $PSCmdlet 'ScopeId and ReservedIP cannot be specified together.' }
}

# ---- Leases ----

# Returns the seeded leases of the scope; -AllLeases is only recorded.
function Get-DhcpServerv4Lease {
    [CmdletBinding()]
    param([Parameter(Position = 0)][ipaddress]$ScopeId, [string]$ComputerName, [switch]$AllLeases)
    Record 'Get-DhcpServerv4Lease' $PSBoundParameters
    $global:Leases | Where-Object { "$($_.ScopeId)" -eq "$ScopeId" }
}

# ======================================================================
# Cmdlets added by netbox-windows-dhcp-plugin (not in the Go stubs)
# ======================================================================

$global:Classes = [System.Collections.ArrayList]::new()
$global:Failovers = [ordered]@{}
$global:SuperscopeMembers = [ordered]@{}
$global:Filters = [ordered]@{}
$global:FilterListState = [pscustomobject]@{ Allow = $false; Deny = $false }
$global:OptionDefs = [System.Collections.ArrayList]::new()
$global:Policies = [System.Collections.ArrayList]::new()
$global:PolicyRanges = [System.Collections.ArrayList]::new()
$global:V6Scopes = [ordered]@{}
$global:V6Reservations = [ordered]@{}
$global:V6Options = [System.Collections.ArrayList]::new()

# ---- Classes ----

function Get-DhcpServerv4Class {
    [CmdletBinding()]
    param([string]$ComputerName, [string[]]$Name, [ValidateSet('Vendor', 'User')][string]$Type)
    Record 'Get-DhcpServerv4Class' $PSBoundParameters
    $global:Classes
}

# Data is stored as hex, with the ASCII form in AsciiData.
function Add-DhcpServerv4Class {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet('Vendor', 'User')][string]$Type,
        [Parameter(Mandatory)][string]$Data, [string]$Description, [switch]$PassThru)
    Record 'Add-DhcpServerv4Class' $PSBoundParameters
    $hex = '0x' + (([Text.Encoding]::ASCII.GetBytes($Data) | ForEach-Object { $_.ToString('x2') }) -join '')
    [void]$global:Classes.Add([pscustomobject]@{ Name = $Name; Type = $Type; Data = $hex; AsciiData = $Data
        Description = $Description })
}

function Set-DhcpServerv4Class {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet('Vendor', 'User')][string]$Type,
        [string]$Data, [string]$Description, [switch]$PassThru)
    Record 'Set-DhcpServerv4Class' $PSBoundParameters
    $c = $global:Classes | Where-Object { $_.Name -eq $Name -and $_.Type -eq $Type } | Select-Object -First 1
    if (-not $c) { Write-DhcpError $PSCmdlet 20049 ObjectNotFound "The class $Name does not exist."; return }
    if ($PSBoundParameters.ContainsKey('Data')) { $c.AsciiData = $Data }
    if ($PSBoundParameters.ContainsKey('Description')) { $c.Description = $Description }
}

function Remove-DhcpServerv4Class {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet('Vendor', 'User')][string]$Type, [switch]$PassThru)
    Record 'Remove-DhcpServerv4Class' $PSBoundParameters
}

# ---- Failover ----

function Get-DhcpServerv4Failover {
    [CmdletBinding()]
    param([string]$ComputerName, [string[]]$Name)
    Record 'Get-DhcpServerv4Failover' $PSBoundParameters
    $global:Failovers.Values
}

function Add-DhcpServerv4Failover {
    [CmdletBinding(SupportsShouldProcess, DefaultParameterSetName = 'LoadBalance')]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$PartnerServer,
        [Parameter(Mandatory)][ipaddress[]]$ScopeId,
        [Parameter(ParameterSetName = 'LoadBalance')][uint32]$LoadBalancePercent,
        [Parameter(Mandatory, ParameterSetName = 'HotStandby')][ValidateSet('Active', 'Standby')][string]$ServerRole,
        [Parameter(ParameterSetName = 'HotStandby')][uint32]$ReservePercent,
        [TimeSpan]$MaxClientLeadTime, [TimeSpan]$StateSwitchInterval, [bool]$AutoStateTransition,
        [string]$SharedSecret, [switch]$Force, [switch]$PassThru)
    Record 'Add-DhcpServerv4Failover' $PSBoundParameters
    $hot = $PSCmdlet.ParameterSetName -eq 'HotStandby'
    $global:Failovers[$Name] = [pscustomobject]@{
        Name = $Name; PartnerServer = $PartnerServer; ScopeId = $ScopeId
        Mode = if ($hot) { 'HotStandby' } else { 'LoadBalance' }
        ServerRole = $ServerRole
        LoadBalancePercent = if ($hot) { $null } elseif ($PSBoundParameters.ContainsKey('LoadBalancePercent')) { $LoadBalancePercent } else { 50 }
        ReservePercent = if (-not $hot) { $null } elseif ($PSBoundParameters.ContainsKey('ReservePercent')) { $ReservePercent } else { 5 }
        MaxClientLeadTime = if ($PSBoundParameters.ContainsKey('MaxClientLeadTime')) { $MaxClientLeadTime } else { [TimeSpan]'01:00:00' }
        StateSwitchInterval = if ($PSBoundParameters.ContainsKey('StateSwitchInterval')) { $StateSwitchInterval } else { $null }
        AutoStateTransition = $AutoStateTransition
        EnableAuth = [bool]$SharedSecret
        State = 'Normal'
    }
}

function Set-DhcpServerv4Failover {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name,
        [ValidateSet('LoadBalance', 'HotStandby')][string]$Mode, [ValidateSet('Active', 'Standby')][string]$ServerRole,
        [uint32]$LoadBalancePercent, [uint32]$ReservePercent, [TimeSpan]$MaxClientLeadTime,
        [TimeSpan]$StateSwitchInterval, [bool]$AutoStateTransition, [string]$SharedSecret,
        [switch]$Force, [switch]$PassThru)
    Record 'Set-DhcpServerv4Failover' $PSBoundParameters
    $f = $global:Failovers[$Name]
    foreach ($k in 'Mode', 'ServerRole', 'LoadBalancePercent', 'ReservePercent', 'MaxClientLeadTime',
        'StateSwitchInterval', 'AutoStateTransition') {
        if ($PSBoundParameters.ContainsKey($k)) { $f.$k = $PSBoundParameters[$k] }
    }
}

function Remove-DhcpServerv4Failover {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string[]]$Name, [switch]$Force, [switch]$PassThru)
    Record 'Remove-DhcpServerv4Failover' $PSBoundParameters
}

function Add-DhcpServerv4FailoverScope {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][ipaddress[]]$ScopeId,
        [switch]$PassThru)
    Record 'Add-DhcpServerv4FailoverScope' $PSBoundParameters
    $f = $global:Failovers[$Name]
    $f.ScopeId = @($f.ScopeId) + $ScopeId
}

function Remove-DhcpServerv4FailoverScope {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][ipaddress[]]$ScopeId,
        [switch]$Force, [switch]$PassThru)
    Record 'Remove-DhcpServerv4FailoverScope' $PSBoundParameters
    $f = $global:Failovers[$Name]
    $ids = @($ScopeId | ForEach-Object { "$_" })
    $f.ScopeId = @($f.ScopeId | Where-Object { $ids -notcontains "$_" })
}

# Like the real cmdlet, writes the replicated scopes to the pipeline (the script must discard them).
function Invoke-DhcpServerv4FailoverReplication {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [string[]]$Name, [ipaddress[]]$ScopeId, [switch]$Force)
    Record 'Invoke-DhcpServerv4FailoverReplication' $PSBoundParameters
    'replicated'
}

# ---- Superscopes ----
# Get returns one object per member scope (the grouping in the script must cope).

function Get-DhcpServerv4Superscope {
    [CmdletBinding()]
    param([string]$ComputerName, [string]$SuperscopeName)
    Record 'Get-DhcpServerv4Superscope' $PSBoundParameters
    foreach ($k in $global:SuperscopeMembers.Keys) {
        foreach ($id in $global:SuperscopeMembers[$k]) { [pscustomobject]@{ SuperscopeName = $k; ScopeId = [ipaddress]$id } }
    }
    [pscustomobject]@{ SuperscopeName = ''; ScopeId = [ipaddress]'10.99.0.0' }
}

function Add-DhcpServerv4Superscope {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$SuperscopeName,
        [Parameter(Mandatory)][ipaddress[]]$ScopeId, [switch]$PassThru)
    Record 'Add-DhcpServerv4Superscope' $PSBoundParameters
    if (-not $global:SuperscopeMembers.Contains($SuperscopeName)) {
        $global:SuperscopeMembers[$SuperscopeName] = [System.Collections.ArrayList]::new()
    }
    foreach ($id in $ScopeId) { [void]$global:SuperscopeMembers[$SuperscopeName].Add("$id") }
}

function Remove-DhcpServerv4Superscope {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$SuperscopeName, [ipaddress[]]$ScopeId,
        [switch]$PassThru)
    Record 'Remove-DhcpServerv4Superscope' $PSBoundParameters
    if ($ScopeId) {
        foreach ($id in $ScopeId) { $global:SuperscopeMembers[$SuperscopeName].Remove("$id") }
        if ($global:SuperscopeMembers[$SuperscopeName].Count -gt 0) { return }
    }
    $global:SuperscopeMembers.Remove($SuperscopeName)
}

# Unlike Add and Remove (-SuperscopeName), Rename takes -Name and -NewName (Get-Command on Windows Server 2025,
# issue #14). Declaring only those makes a script passing -SuperscopeName fail here as it does on Windows.
function Rename-DhcpServerv4Superscope {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory, Position = 0)][string]$Name,
        [Parameter(Mandatory, Position = 1)][string]$NewName, [switch]$PassThru)
    Record 'Rename-DhcpServerv4Superscope' $PSBoundParameters
    $global:SuperscopeMembers[$NewName] = $global:SuperscopeMembers[$Name]
    $global:SuperscopeMembers.Remove($Name)
}

# ---- MAC filters ----

function Get-DhcpServerv4Filter {
    [CmdletBinding()]
    param([string]$ComputerName, [ValidateSet('Allow', 'Deny')][string]$List)
    Record 'Get-DhcpServerv4Filter' $PSBoundParameters
    $global:Filters.Values | Where-Object { -not $List -or $_.List -eq $List }
}

function Add-DhcpServerv4Filter {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][ValidateSet('Allow', 'Deny')][string]$List,
        [Parameter(Mandatory)][string[]]$MacAddress, [string]$Description, [switch]$Force, [switch]$PassThru)
    Record 'Add-DhcpServerv4Filter' $PSBoundParameters
    foreach ($m in $MacAddress) {
        $global:Filters[$m.ToLower()] = [pscustomobject]@{ MacAddress = $m.ToLower(); List = $List; Description = $Description }
    }
}

function Remove-DhcpServerv4Filter {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string[]]$MacAddress, [switch]$PassThru)
    Record 'Remove-DhcpServerv4Filter' $PSBoundParameters
    foreach ($m in $MacAddress) { $global:Filters.Remove($m.ToLower()) }
}

function Get-DhcpServerv4FilterList {
    [CmdletBinding()]
    param([string]$ComputerName)
    Record 'Get-DhcpServerv4FilterList' $PSBoundParameters
    $global:FilterListState
}

function Set-DhcpServerv4FilterList {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [bool]$Allow, [bool]$Deny, [switch]$PassThru)
    Record 'Set-DhcpServerv4FilterList' $PSBoundParameters
    if ($PSBoundParameters.ContainsKey('Allow')) { $global:FilterListState.Allow = $Allow }
    if ($PSBoundParameters.ContainsKey('Deny')) { $global:FilterListState.Deny = $Deny }
}

# ---- Option definitions ----

function Get-DhcpServerv4OptionDefinition {
    [CmdletBinding()]
    param([string]$ComputerName, [uint32[]]$OptionId, [string]$VendorClass, [switch]$All)
    Record 'Get-DhcpServerv4OptionDefinition' $PSBoundParameters
    $global:OptionDefs | Where-Object { $All -or "$($_.VendorClass)" -eq "$VendorClass" }
}

function Add-DhcpServerv4OptionDefinition {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][uint32]$OptionId,
        [Parameter(Mandatory)][ValidateSet('Byte', 'Word', 'DWord', 'DWordDword', 'IPv4Address', 'String',
            'BinaryData', 'EncapsulatedData', 'IPv6Address')][string]$Type,
        [string]$VendorClass, [string]$Description, [switch]$MultiValued, [string[]]$DefaultValue, [switch]$PassThru)
    Record 'Add-DhcpServerv4OptionDefinition' $PSBoundParameters
    [void]$global:OptionDefs.Add([pscustomobject]@{ OptionId = $OptionId; Name = $Name; Type = $Type
        VendorClass = $VendorClass; Description = $Description; MultiValued = $MultiValued.IsPresent
        DefaultValue = $DefaultValue })
}

function Set-DhcpServerv4OptionDefinition {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][uint32]$OptionId, [string]$Name, [string]$Description,
        [string]$VendorClass, [string[]]$DefaultValue, [switch]$PassThru)
    Record 'Set-DhcpServerv4OptionDefinition' $PSBoundParameters
    $d = $global:OptionDefs | Where-Object { $_.OptionId -eq $OptionId -and "$($_.VendorClass)" -eq "$VendorClass" } |
        Select-Object -First 1
    if ($Name) { $d.Name = $Name }
    if ($PSBoundParameters.ContainsKey('Description')) { $d.Description = $Description }
}

function Remove-DhcpServerv4OptionDefinition {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][uint32]$OptionId, [string]$VendorClass, [switch]$PassThru)
    Record 'Remove-DhcpServerv4OptionDefinition' $PSBoundParameters
}

# ---- Policies ----

function Get-DhcpServerv4Policy {
    [CmdletBinding()]
    param([string]$ComputerName, [string[]]$Name, [ipaddress]$ScopeId)
    Record 'Get-DhcpServerv4Policy' $PSBoundParameters
    $global:Policies | Where-Object { "$($_.ScopeIdKey)" -eq "$ScopeId" }
}

function Add-DhcpServerv4Policy {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][ValidateSet('AND', 'OR')][string]$Condition,
        [ipaddress]$ScopeId, [string]$Description, [uint16]$ProcessingOrder, [bool]$Enabled = $true,
        [string[]]$VendorClass, [string[]]$UserClass, [string[]]$MacAddress, [string[]]$ClientId,
        [string[]]$RelayAgent, [string[]]$CircuitId, [string[]]$RemoteId, [string[]]$SubscriberId, [string[]]$Fqdn,
        [TimeSpan]$LeaseDuration, [switch]$PassThru)
    Record 'Add-DhcpServerv4Policy' $PSBoundParameters
    # Windows numbers the policies of a level 1, 2, 3... without gaps: a number past the end is DHCP 20110 (seen on
    # Windows Server 2025), a used one moves the others down, none means the end of the list.
    $level = @($global:Policies | Where-Object { "$($_.ScopeIdKey)" -eq "$ScopeId" })
    $order = if ($PSBoundParameters.ContainsKey('ProcessingOrder')) { [int]$ProcessingOrder } else { $level.Count + 1 }
    if ($order -lt 1 -or $order -gt $level.Count + 1) {
        Write-DhcpError $PSCmdlet 20110 InvalidData "Failed to create policy $Name on DHCP server STUB."
        return
    }
    foreach ($x in $level) { if ($x.ProcessingOrder -ge $order) { $x.ProcessingOrder = $x.ProcessingOrder + 1 } }
    [void]$global:Policies.Add([pscustomobject]@{ Name = $Name; ScopeIdKey = "$ScopeId"; Condition = $Condition
        Description = $Description; ProcessingOrder = $order
        Enabled = $Enabled; VendorClass = $VendorClass; UserClass = $UserClass; MacAddress = $MacAddress
        ClientId = $ClientId; RelayAgent = $RelayAgent; CircuitId = $CircuitId; RemoteId = $RemoteId
        SubscriberId = $SubscriberId; Fqdn = $Fqdn; LeaseDuration = $LeaseDuration })
}

function Set-DhcpServerv4Policy {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name, [ipaddress]$ScopeId, [string]$NewName,
        [ValidateSet('AND', 'OR')][string]$Condition, [string]$Description, [uint16]$ProcessingOrder, [bool]$Enabled,
        [string[]]$VendorClass, [string[]]$UserClass, [string[]]$MacAddress, [string[]]$ClientId,
        [string[]]$RelayAgent, [string[]]$CircuitId, [string[]]$RemoteId, [string[]]$SubscriberId, [string[]]$Fqdn,
        [TimeSpan]$LeaseDuration, [switch]$PassThru)
    Record 'Set-DhcpServerv4Policy' $PSBoundParameters
    $x = $global:Policies | Where-Object { $_.Name -eq $Name -and "$($_.ScopeIdKey)" -eq "$ScopeId" } | Select-Object -First 1
    foreach ($k in $PSBoundParameters.Keys) {
        if ($commonParams -contains $k -or $k -in 'Name', 'ScopeId', 'ComputerName', 'PassThru', 'NewName') { continue }
        $x.$k = $PSBoundParameters[$k]
    }
}

function Remove-DhcpServerv4Policy {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name, [ipaddress]$ScopeId, [switch]$PassThru)
    Record 'Remove-DhcpServerv4Policy' $PSBoundParameters
}

function Get-DhcpServerv4PolicyIPRange {
    [CmdletBinding()]
    param([string]$ComputerName, [Parameter(Mandatory)][ipaddress]$ScopeId, [Parameter(Mandatory)][string]$Name)
    Record 'Get-DhcpServerv4PolicyIPRange' $PSBoundParameters
    $global:PolicyRanges | Where-Object { "$($_.ScopeId)" -eq "$ScopeId" -and $_.Name -eq $Name }
}

function Add-DhcpServerv4PolicyIPRange {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][ipaddress]$ScopeId, [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ipaddress]$StartRange, [Parameter(Mandatory)][ipaddress]$EndRange, [switch]$PassThru)
    Record 'Add-DhcpServerv4PolicyIPRange' $PSBoundParameters
    [void]$global:PolicyRanges.Add([pscustomobject]@{ ScopeId = $ScopeId; Name = $Name; StartRange = $StartRange
        EndRange = $EndRange })
}

function Remove-DhcpServerv4PolicyIPRange {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][ipaddress]$ScopeId, [Parameter(Mandatory)][string]$Name,
        [ipaddress]$StartRange, [ipaddress]$EndRange, [switch]$PassThru)
    Record 'Remove-DhcpServerv4PolicyIPRange' $PSBoundParameters
}

# ---- DHCPv6 ----

function Get-DhcpServerv6Scope {
    [CmdletBinding()]
    param([string]$ComputerName, [ipaddress[]]$Prefix)
    Record 'Get-DhcpServerv6Scope' $PSBoundParameters
    $global:V6Scopes.Values
}

function Add-DhcpServerv6Scope {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][ipaddress]$Prefix, [Parameter(Mandatory)][string]$Name,
        [string]$Description, [ValidateSet('Active', 'InActive')][string]$State = 'Active', [uint16]$Preference,
        [TimeSpan]$PreferredLifeTime = '8.00:00:00', [TimeSpan]$ValidLifeTime = '12.00:00:00',
        [TimeSpan]$T1 = '4.00:00:00', [TimeSpan]$T2 = '6.09:36:00', [switch]$PassThru)
    Record 'Add-DhcpServerv6Scope' $PSBoundParameters
    $global:V6Scopes["$Prefix"] = [pscustomobject]@{ Prefix = $Prefix; Name = $Name; Description = $Description
        State = if ($State -eq 'InActive') { 'Inactive' } else { $State }; Preference = $Preference
        PreferredLifetime = $PreferredLifeTime; ValidLifetime = $ValidLifeTime; T1 = $T1; T2 = $T2 }
}

function Set-DhcpServerv6Scope {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][ipaddress]$Prefix, [string]$Name, [string]$Description,
        [ValidateSet('Active', 'InActive')][string]$State, [uint16]$Preference, [TimeSpan]$PreferredLifeTime,
        [TimeSpan]$ValidLifeTime, [TimeSpan]$T1, [TimeSpan]$T2, [switch]$PassThru)
    Record 'Set-DhcpServerv6Scope' $PSBoundParameters
    $s = $global:V6Scopes["$Prefix"]
    if ($Name) { $s.Name = $Name }
    if ($PSBoundParameters.ContainsKey('Description')) { $s.Description = $Description }
    if ($PSBoundParameters.ContainsKey('Preference')) { $s.Preference = $Preference }
}

function Remove-DhcpServerv6Scope {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][ipaddress[]]$Prefix, [switch]$Force, [switch]$PassThru)
    Record 'Remove-DhcpServerv6Scope' $PSBoundParameters
}

function Get-DhcpServerv6Reservation {
    [CmdletBinding()]
    param([string]$ComputerName, [ipaddress]$Prefix, [ipaddress[]]$IPAddress)
    Record 'Get-DhcpServerv6Reservation' $PSBoundParameters
    $global:V6Reservations.Values | Where-Object { "$($_.Prefix)" -eq "$Prefix" }
}

function Add-DhcpServerv6Reservation {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][ipaddress]$Prefix, [Parameter(Mandatory)][ipaddress]$IPAddress,
        [Parameter(Mandatory)][string]$ClientDuid, [Parameter(Mandatory)][uint32]$Iaid, [string]$Name,
        [string]$Description, [switch]$PassThru)
    Record 'Add-DhcpServerv6Reservation' $PSBoundParameters
    if (-not $Name) { $Name = "$IPAddress" }
    $global:V6Reservations["$IPAddress"] = [pscustomobject]@{ Prefix = $Prefix; IPAddress = $IPAddress
        ClientDuid = $ClientDuid; Iaid = $Iaid; Name = $Name; Description = $Description }
}

function Set-DhcpServerv6Reservation {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][ipaddress]$IPAddress, [string]$ClientDuid, [uint32]$Iaid,
        [string]$Name, [string]$Description, [switch]$PassThru)
    Record 'Set-DhcpServerv6Reservation' $PSBoundParameters
}

function Remove-DhcpServerv6Reservation {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [ipaddress]$Prefix, [ipaddress[]]$IPAddress, [switch]$PassThru)
    Record 'Remove-DhcpServerv6Reservation' $PSBoundParameters
}

function Get-DhcpServerv6OptionValue {
    [CmdletBinding()]
    param([string]$ComputerName, [ipaddress]$Prefix, [ipaddress]$ReservedIP, [uint32[]]$OptionId,
        [string]$VendorClass, [string]$UserClass, [switch]$All)
    Record 'Get-DhcpServerv6OptionValue' $PSBoundParameters
    # Like IPv4: Get tolerates -Prefix next to -ReservedIP; Set and Remove refuse the pair (issue #17).
    $level = if ($ReservedIP) { "$ReservedIP" } else { "$Prefix" }
    $global:V6Options | Where-Object { "$($_.Level)" -eq $level }
}

function Set-DhcpServerv6OptionValue {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [ipaddress]$Prefix, [ipaddress]$ReservedIP, [Parameter(Mandatory)][uint32]$OptionId,
        [Parameter(Mandatory)][string[]]$Value, [string]$VendorClass, [string]$UserClass, [switch]$Force,
        [switch]$PassThru)
    Record 'Set-DhcpServerv6OptionValue' $PSBoundParameters
    if ($Prefix -and $ReservedIP) {
        Write-InvalidArgument $PSCmdlet 'Prefix and ReservedIP cannot be specified together.'
        return
    }
    [void]$global:V6Options.Add([pscustomobject]@{ Level = "$Prefix$ReservedIP"; OptionId = $OptionId
        Name = 'DNS Recursive Name Server IPv6 Address List'; Value = $Value; VendorClass = $VendorClass
        UserClass = $UserClass })
}

function Remove-DhcpServerv6OptionValue {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [ipaddress]$Prefix, [ipaddress]$ReservedIP, [Parameter(Mandatory)][uint32[]]$OptionId,
        [string]$VendorClass, [string]$UserClass, [switch]$PassThru)
    Record 'Remove-DhcpServerv6OptionValue' $PSBoundParameters
    if ($Prefix -and $ReservedIP) { Write-InvalidArgument $PSCmdlet 'Prefix and ReservedIP cannot be specified together.' }
}

$global:V6Classes = [System.Collections.ArrayList]::new()
$global:V6Exclusions = [System.Collections.ArrayList]::new()
$global:V6Leases = [System.Collections.ArrayList]::new()

function Get-DhcpServerv6Class {
    [CmdletBinding()]
    param([string]$ComputerName, [Parameter(Position = 0)][string[]]$Name, [ValidateSet('Vendor', 'User')][string]$Type)
    Record 'Get-DhcpServerv6Class' $PSBoundParameters
    $global:V6Classes
}

function Add-DhcpServerv6Class {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [string]$Description, [uint32]$VendorId, [Parameter(Mandatory)][string]$Data,
        [Parameter(Mandatory)][ValidateSet('Vendor', 'User')][string]$Type, [Parameter(Mandatory)][string]$Name,
        [switch]$PassThru)
    Record 'Add-DhcpServerv6Class' $PSBoundParameters
    [void]$global:V6Classes.Add([pscustomobject]@{ Name = $Name; Type = $Type; Data = $Data
        AsciiData = $Data; Description = $Description; VendorId = $VendorId })
}

function Set-DhcpServerv6Class {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet('Vendor', 'User')][string]$Type, [string]$Data, [string]$Description,
        [switch]$PassThru)
    Record 'Set-DhcpServerv6Class' $PSBoundParameters
    $c = $global:V6Classes | Where-Object { $_.Name -eq $Name -and $_.Type -eq $Type } | Select-Object -First 1
    if ($PSBoundParameters.ContainsKey('Data')) { $c.Data = $Data; $c.AsciiData = $Data }
    if ($PSBoundParameters.ContainsKey('Description')) { $c.Description = $Description }
}

function Remove-DhcpServerv6Class {
    [CmdletBinding(SupportsShouldProcess)]
    param([string]$ComputerName, [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet('Vendor', 'User')][string]$Type, [switch]$PassThru)
    Record 'Remove-DhcpServerv6Class' $PSBoundParameters
}

function Get-DhcpServerv6ExclusionRange {
    [CmdletBinding()]
    param([string]$ComputerName, [Parameter(Position = 0)][ipaddress[]]$Prefix)
    Record 'Get-DhcpServerv6ExclusionRange' $PSBoundParameters
    $global:V6Exclusions | Where-Object { "$($_.Prefix)" -eq "$Prefix" }
}

# Seeded leases carry the properties a real Get-DhcpServerv6Lease returns on Windows Server 2025 (issue #22):
# IPAddress, Prefix, AddressType, ClientDuid, Description, HostName, Iaid and LeaseExpiryTime. There is no
# AddressState, unlike the IPv4 lease.
function Get-DhcpServerv6Lease {
    [CmdletBinding()]
    param([string]$ComputerName, [Parameter(Position = 0)][ipaddress]$Prefix, [ipaddress[]]$IPAddress)
    Record 'Get-DhcpServerv6Lease' $PSBoundParameters
    $global:V6Leases | Where-Object { "$($_.Prefix)" -eq "$Prefix" }
}
