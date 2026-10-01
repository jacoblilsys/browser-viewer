$ErrorActionPreference = 'Stop'

$python = 'C:\Program Files\Python314\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python executable not found: $python"
}

$scope = @{
    Direction       = 'Inbound'
    Action          = 'Allow'
    Program         = $python
    Profile         = 'Public'
    InterfaceAlias  = 'Ethernet 2'
    RemoteAddress   = '192.168.137.0/24'
}

# Remove only our three precisely named rules before recreating them. This makes
# rerunning the installer update an old link-local scope to the current ICS
# subnet instead of silently retaining stale filters.
$ruleNames = @(
    'A2E Python TCP Stream',
    'A2E Python UDP Data and mDNS',
    'A2E Python UDP API Replies'
)
Get-NetFirewallRule -DisplayName $ruleNames -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule

New-NetFirewallRule -DisplayName 'A2E Python TCP Stream' `
    -Protocol TCP -LocalPort 8066 @scope | Out-Null

New-NetFirewallRule -DisplayName 'A2E Python UDP Data and mDNS' `
    -Protocol UDP -LocalPort 5353,8066 @scope | Out-Null

New-NetFirewallRule -DisplayName 'A2E Python UDP API Replies' `
    -Protocol UDP -RemotePort 56671 @scope | Out-Null

Write-Host 'A2E firewall rules installed successfully.' -ForegroundColor Green
Get-NetFirewallRule -DisplayName `
    'A2E Python TCP Stream', `
    'A2E Python UDP Data and mDNS', `
    'A2E Python UDP API Replies' |
    Select-Object DisplayName, Enabled, Profile, Direction, Action |
    Format-Table -AutoSize
