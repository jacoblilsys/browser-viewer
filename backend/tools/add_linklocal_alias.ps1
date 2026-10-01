[CmdletBinding()]
param(
    [string]$InterfaceAlias = 'Ethernet 2',
    [string]$IPAddress = '169.254.96.250',
    [int]$PrefixLength = 16
)

$ErrorActionPreference = 'Stop'

$existing = Get-NetIPAddress `
    -InterfaceAlias $InterfaceAlias `
    -AddressFamily IPv4 `
    -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -eq $IPAddress }

if (-not $existing) {
    New-NetIPAddress `
        -InterfaceAlias $InterfaceAlias `
        -IPAddress $IPAddress `
        -PrefixLength $PrefixLength | Out-Null
}

Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 |
    Select-Object IPAddress, PrefixLength, AddressState |
    Format-Table -AutoSize
