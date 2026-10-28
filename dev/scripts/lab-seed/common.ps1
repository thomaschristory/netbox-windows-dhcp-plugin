# Lab seed, both servers: classes and option definitions. Failover replication and the policies expect them on each
# partner, as an administrator would keep them. Safe to run again: existing ones are left alone.
$log = [System.Collections.Generic.List[string]]::new()
$classes = @(
    @{ Name = 'Printers'; Type = 'User'; Data = 'printers'; Description = 'Network printers' }
    @{ Name = 'Lab-Kiosks'; Type = 'User'; Data = 'kiosk'; Description = 'Self-service kiosks' }
    @{ Name = 'Polycom'; Type = 'Vendor'; Data = 'Polycom'; Description = 'Polycom desk phones' }
)
foreach ($c in $classes) {
    if (-not (Get-DhcpServerv4Class -Name $c.Name -ErrorAction SilentlyContinue)) {
        Add-DhcpServerv4Class @c
        $log.Add("class $($c.Type) $($c.Name)")
    }
}
if (-not (Get-DhcpServerv4OptionDefinition -OptionId 200 -ErrorAction SilentlyContinue)) {
    Add-DhcpServerv4OptionDefinition -Name 'Asset Server' -OptionId 200 -Type IPv4Address -Description 'Inventory agent target'
    $log.Add('option definition 200 Asset Server')
}
if (-not (Get-DhcpServerv4OptionDefinition -VendorClass 'Polycom' -OptionId 1 -ErrorAction SilentlyContinue)) {
    Add-DhcpServerv4OptionDefinition -Name 'Provisioning URL' -OptionId 1 -Type String -VendorClass 'Polycom' -Description 'Phone provisioning server'
    $log.Add('option definition Polycom/1 Provisioning URL')
}
$out = $log
