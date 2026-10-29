# Lab seed, either server: removes what the seed created, and nothing else (objects are matched by the seed's names,
# scope IDs and MAC addresses). Run on the first server, then the second. Removing a failover relationship on the first
# server also removes it, and its scopes, from the partner.
$log = [System.Collections.Generic.List[string]]::new()
foreach ($name in 'campus-lb', 'voice-hs') {
    if (Get-DhcpServerv4Failover -Name $name -ErrorAction SilentlyContinue) {
        Remove-DhcpServerv4Failover -Name $name -Confirm:$false
        $log.Add("failover $name")
    }
}
if (Get-DhcpServerv4Policy -Name 'Kiosks' -ErrorAction SilentlyContinue) {
    Remove-DhcpServerv4Policy -Name 'Kiosks'
    $log.Add('server policy Kiosks')
}
if (Get-DhcpServerv4Superscope -SuperscopeName 'Campus-VLAN10' -ErrorAction SilentlyContinue | Where-Object SuperscopeName) {
    Remove-DhcpServerv4Superscope -SuperscopeName 'Campus-VLAN10' -Confirm:$false
    $log.Add('superscope Campus-VLAN10')
}
foreach ($id in '198.18.10.0', '198.18.11.0', '198.18.20.0', '198.18.30.0', '198.18.40.0', '198.18.50.0', '198.18.90.0') {
    if (Get-DhcpServerv4Scope -ScopeId $id -ErrorAction SilentlyContinue) {
        Remove-DhcpServerv4Scope -ScopeId $id -Force -Confirm:$false
        $log.Add("scope $id")
    }
}
if (Get-DhcpServerv6Scope -Prefix 2001:db8:4e43:: -ErrorAction SilentlyContinue) {
    Remove-DhcpServerv6Scope -Prefix 2001:db8:4e43:: -Force -Confirm:$false
    $log.Add('v6 scope 2001:db8:4e43::')
}
# Server options: only when they still hold the seed's value.
$seeded = @{
    6 = @('198.18.0.53', '198.18.0.54'); 15 = @('corp.example', 'lab.corp.example'); 42 = @('198.18.0.123')
    200 = @('198.18.0.200')
}
foreach ($o in @(Get-DhcpServerv4OptionValue)) {
    $known = $seeded[[int]$o.OptionId]
    if ($known -and -not @($o.Value | Where-Object { $known -notcontains $_ }).Count) {
        Remove-DhcpServerv4OptionValue -OptionId $o.OptionId -Confirm:$false
        $log.Add("server option $($o.OptionId)")
    }
}
if (Get-DhcpServerv4OptionDefinition -OptionId 200 -ErrorAction SilentlyContinue | Where-Object Name -eq 'Asset Server') {
    Remove-DhcpServerv4OptionDefinition -OptionId 200
    $log.Add('option definition 200')
}
# Removing the vendor class also removes its option definitions.
foreach ($name in 'Printers', 'Lab-Kiosks', 'Polycom') {
    $c = Get-DhcpServerv4Class -Name $name -ErrorAction SilentlyContinue
    if ($c) {
        Remove-DhcpServerv4Class -Name $name -Type $c.Type
        $log.Add("class $name")
    }
}
$macs = '00-15-5d-de-ad-01', '00-15-5d-de-ad-02', '00-15-5d-10-00-25'
foreach ($f in @(Get-DhcpServerv4Filter | Where-Object { $macs -contains "$($_.MacAddress)".ToLower() })) {
    Remove-DhcpServerv4Filter -MacAddress $f.MacAddress
    $log.Add("MAC filter $($f.MacAddress)")
}
if (-not @(Get-DhcpServerv4Filter -List Deny -ErrorAction SilentlyContinue).Count) {
    Set-DhcpServerv4FilterList -Deny $false
}
$out = $log
