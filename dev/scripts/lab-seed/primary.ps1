# Lab seed, first server: a realistic configuration with every kind the plugin imports. Needs CredSSP for the failover
# part. Parameters ($p): partner (the second server, as this one reaches it), shared_secret (failover message
# authentication). Values only ever come from $p.
$log = [System.Collections.Generic.List[string]]::new()
foreach ($id in '198.18.10.0', '198.18.11.0', '198.18.20.0', '198.18.30.0', '198.18.40.0', '198.18.50.0') {
    if (Get-DhcpServerv4Scope -ScopeId $id -ErrorAction SilentlyContinue) {
        throw "scope $id already exists: run make lab-wipe first"
    }
}

# Server options and a server-level policy.
Set-DhcpServerv4OptionValue -OptionId 6 -Value '198.18.0.53', '198.18.0.54' -Force
Set-DhcpServerv4OptionValue -OptionId 15 -Value 'corp.example'
Set-DhcpServerv4OptionValue -OptionId 42 -Value '198.18.0.123'
Set-DhcpServerv4OptionValue -OptionId 200 -Value '198.18.0.200'
Add-DhcpServerv4Policy -Name 'Kiosks' -Condition OR -UserClass EQ, 'Lab-Kiosks' -Description 'Kiosks get their own domain'
Set-DhcpServerv4OptionValue -PolicyName 'Kiosks' -OptionId 15 -Value 'kiosk.corp.example'
$log.Add('server options 6, 15, 42, 200; server policy Kiosks')

# VLAN10: two subnets on one segment, in a superscope.
Add-DhcpServerv4Scope -Name 'VLAN10-Users' -StartRange 198.18.10.20 -EndRange 198.18.10.240 -SubnetMask 255.255.255.0 -LeaseDuration 08:00:00 -Description 'Office users, first subnet'
Add-DhcpServerv4ExclusionRange -ScopeId 198.18.10.0 -StartRange 198.18.10.200 -EndRange 198.18.10.220
Set-DhcpServerv4OptionValue -ScopeId 198.18.10.0 -OptionId 3 -Value '198.18.10.1'
Add-DhcpServerv4Reservation -ScopeId 198.18.10.0 -IPAddress 198.18.10.25 -ClientId '00-15-5d-10-00-25' -Name 'laptop-ceo' -Description 'CEO laptop'
Add-DhcpServerv4Reservation -ScopeId 198.18.10.0 -IPAddress 198.18.10.26 -ClientId '00-15-5d-10-00-26' -Name 'laptop-cfo'
Add-DhcpServerv4Scope -Name 'VLAN10-Users-B' -StartRange 198.18.11.20 -EndRange 198.18.11.240 -SubnetMask 255.255.255.0 -LeaseDuration 08:00:00 -Description 'Office users, second subnet'
Set-DhcpServerv4OptionValue -ScopeId 198.18.11.0 -OptionId 3 -Value '198.18.11.1'
Add-DhcpServerv4Superscope -SuperscopeName 'Campus-VLAN10' -ScopeId 198.18.10.0, 198.18.11.0
$log.Add('VLAN10-Users and VLAN10-Users-B in superscope Campus-VLAN10')

# VLAN20: phones, with a vendor class option and a scope policy that has its own address range.
Add-DhcpServerv4Scope -Name 'VLAN20-Voice' -StartRange 198.18.20.50 -EndRange 198.18.20.250 -SubnetMask 255.255.255.0 -LeaseDuration 1.00:00:00 -Description 'IP phones'
Set-DhcpServerv4OptionValue -ScopeId 198.18.20.0 -OptionId 3 -Value '198.18.20.1'
Set-DhcpServerv4OptionValue -ScopeId 198.18.20.0 -OptionId 66 -Value 'tftp.corp.example'
Set-DhcpServerv4OptionValue -ScopeId 198.18.20.0 -VendorClass 'Polycom' -OptionId 1 -Value 'https://prov.corp.example/polycom'
Add-DhcpServerv4Policy -Name 'Polycom-Phones' -ScopeId 198.18.20.0 -Condition OR -VendorClass EQ, 'Polycom*' -Description 'Polycom phones use the top of the pool'
Add-DhcpServerv4PolicyIPRange -ScopeId 198.18.20.0 -Name 'Polycom-Phones' -StartRange 198.18.20.200 -EndRange 198.18.20.250
Set-DhcpServerv4OptionValue -ScopeId 198.18.20.0 -PolicyName 'Polycom-Phones' -OptionId 66 -Value 'tftp-polycom.corp.example'
$log.Add('VLAN20-Voice with vendor class option and policy Polycom-Phones')

# VLAN30: DHCP and BOOTP, a user class option, and the three reservation types.
Add-DhcpServerv4Scope -Name 'VLAN30-Printers' -StartRange 198.18.30.10 -EndRange 198.18.30.100 -SubnetMask 255.255.255.0 -LeaseDuration 30.00:00:00 -Type Both -Description 'Printers and label printers'
Set-DhcpServerv4OptionValue -ScopeId 198.18.30.0 -OptionId 3 -Value '198.18.30.1'
Set-DhcpServerv4OptionValue -ScopeId 198.18.30.0 -UserClass 'Printers' -OptionId 15 -Value 'print.corp.example'
Add-DhcpServerv4Reservation -ScopeId 198.18.30.0 -IPAddress 198.18.30.11 -ClientId '00-15-5d-30-00-11' -Name 'printer-hr' -Type Both
Add-DhcpServerv4Reservation -ScopeId 198.18.30.0 -IPAddress 198.18.30.12 -ClientId '00-15-5d-30-00-12' -Name 'printer-finance' -Type Dhcp
Set-DhcpServerv4OptionValue -ReservedIP 198.18.30.12 -OptionId 6 -Value '198.18.30.53' -Force
Add-DhcpServerv4Reservation -ScopeId 198.18.30.0 -IPAddress 198.18.30.13 -ClientId '00-15-5d-30-00-13' -Name 'label-printer' -Type Bootp -Description 'Old BOOTP-only label printer'
$log.Add('VLAN30-Printers with user class option and DHCP, BOOTP and Both reservations')

# VLAN40: an inactive scope. VLAN50: reservations only.
Add-DhcpServerv4Scope -Name 'VLAN40-Guests' -StartRange 198.18.40.10 -EndRange 198.18.40.250 -SubnetMask 255.255.255.0 -LeaseDuration 02:00:00 -State InActive -Description 'Guest wifi, off until the event'
Set-DhcpServerv4OptionValue -ScopeId 198.18.40.0 -OptionId 3 -Value '198.18.40.1'
Set-DhcpServerv4OptionValue -ScopeId 198.18.40.0 -OptionId 6 -Value '198.18.40.53' -Force
Add-DhcpServerv4Scope -Name 'VLAN50-Servers' -StartRange 198.18.50.100 -EndRange 198.18.50.150 -SubnetMask 255.255.255.0 -LeaseDuration 8.00:00:00
Set-DhcpServerv4OptionValue -ScopeId 198.18.50.0 -OptionId 3 -Value '198.18.50.1'
Add-DhcpServerv4Reservation -ScopeId 198.18.50.0 -IPAddress 198.18.50.101 -ClientId '00-15-5d-50-01-01' -Name 'app01' -Description 'Application server'
Add-DhcpServerv4Reservation -ScopeId 198.18.50.0 -IPAddress 198.18.50.102 -ClientId '00-15-5d-50-01-02' -Name 'app02'
$log.Add('VLAN40-Guests (inactive) and VLAN50-Servers')

# MAC filters: the deny list on, the allow list off (turning it on would refuse every other client).
Add-DhcpServerv4Filter -List Deny -MacAddress '00-15-5d-de-ad-01' -Description 'Reported stolen'
Add-DhcpServerv4Filter -List Deny -MacAddress '00-15-5d-de-ad-02' -Description 'Rogue access point'
Add-DhcpServerv4Filter -List Allow -MacAddress '00-15-5d-10-00-25' -Description 'CEO laptop'
Set-DhcpServerv4FilterList -Allow $false -Deny $true
$log.Add('MAC filters: 2 deny (deny list on), 1 allow')

# DHCPv6 (not 2001:db8:4e42::/64, which the lab tests use).
Add-DhcpServerv6Scope -Prefix 2001:db8:4e43:: -Name 'v6-Users' -PreferredLifetime 8.00:00:00 -ValidLifetime 12.00:00:00 -Description 'Office users, IPv6'
Set-DhcpServerv6OptionValue -Prefix 2001:db8:4e43:: -OptionId 23 -Value '2001:db8:4e43::53' -Force
Set-DhcpServerv6OptionValue -Prefix 2001:db8:4e43:: -OptionId 24 -Value 'corp.example'
Add-DhcpServerv6Reservation -Prefix 2001:db8:4e43:: -IPAddress 2001:db8:4e43::10 -ClientDuid '00-01-00-01-2a-00-00-00-00-00-00-10' -Iaid 1 -Name 'v6-laptop-ceo'
$log.Add('DHCPv6 v6-Users 2001:db8:4e43::/64')

# Failover: load balance for the user subnets, hot standby (this server active) for voice and printers. Windows copies
# the scopes, their reservations, options and policies to the partner.
Add-DhcpServerv4Failover -Name 'campus-lb' -PartnerServer $p.partner -ScopeId 198.18.10.0, 198.18.11.0 -LoadBalancePercent 50 -MaxClientLeadTime 01:00:00 -StateSwitchInterval 01:00:00 -SharedSecret $p.shared_secret -Force
Add-DhcpServerv4Failover -Name 'voice-hs' -PartnerServer $p.partner -ScopeId 198.18.20.0, 198.18.30.0 -ServerRole Active -ReservePercent 5 -MaxClientLeadTime 01:00:00 -StateSwitchInterval 01:00:00 -SharedSecret $p.shared_secret -Force
$log.Add('failover campus-lb (load balance) and voice-hs (hot standby, this server active)')
$out = $log
