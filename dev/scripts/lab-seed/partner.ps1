# Lab seed, second server: what lives there only, so each server's import shows its own configuration. The failover
# scopes arrive from the first server.
$log = [System.Collections.Generic.List[string]]::new()
if (Get-DhcpServerv4Scope -ScopeId 198.18.90.0 -ErrorAction SilentlyContinue) {
    throw 'scope 198.18.90.0 already exists: run make lab-wipe first'
}
Set-DhcpServerv4OptionValue -OptionId 15 -Value 'lab.corp.example'
Add-DhcpServerv4Scope -Name 'VLAN90-Lab' -StartRange 198.18.90.10 -EndRange 198.18.90.99 -SubnetMask 255.255.255.0 -LeaseDuration 04:00:00 -Description 'Test bench, served by this server only'
Set-DhcpServerv4OptionValue -ScopeId 198.18.90.0 -OptionId 3 -Value '198.18.90.1'
Add-DhcpServerv4ExclusionRange -ScopeId 198.18.90.0 -StartRange 198.18.90.50 -EndRange 198.18.90.59
Add-DhcpServerv4Reservation -ScopeId 198.18.90.0 -IPAddress 198.18.90.20 -ClientId '00-15-5d-90-00-20' -Name 'bench-switch'
$log.Add('server option 15; VLAN90-Lab with exclusion and reservation')
$out = $log
