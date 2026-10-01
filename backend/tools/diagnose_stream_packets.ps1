# Capture the sensor stream port with PktMon while asking the viewer backend to
# start raw and FFT streaming. Run from an elevated PowerShell:
#   .\diagnose_stream_packets.ps1 -SensorIp <ip> -Mac <mac> -Password <app-password> [-Server <viewer-ip>]
param(
    [Parameter(Mandatory)] [string] $SensorIp,
    [Parameter(Mandatory)] [string] $Mac,
    [Parameter(Mandatory)] [string] $Password,
    [string] $Server = '127.0.0.1'
)
$ErrorActionPreference = 'Stop'

$etl = Join-Path $PSScriptRoot 'a2e_stream_capture.etl'
$txt = Join-Path $PSScriptRoot 'a2e_stream_capture.txt'
$filterName = 'A2EStream8066'

try {
    $status = (& pktmon status 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read PktMon status: $status"
    }
    if ($status -notmatch 'not running' -and $status -match 'running') {
        $status | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'pktmon_status.txt')
        throw 'PktMon is already capturing; refusing to disturb the existing capture.'
    }

    & pktmon filter remove $filterName 2>$null
    & pktmon filter add $filterName -p 8066 | Out-Null
    try {
        & pktmon start --capture --pkt-size 0 --file-name $etl | Out-Null
        $body = @{
            target_ip = $SensorIp
            mac       = $Mac
            password  = $Password
        } | ConvertTo-Json
        Invoke-RestMethod -Method Post `
            -Uri "http://${Server}:8000/api/stream/raw/start" `
            -ContentType 'application/json' -Body $body -TimeoutSec 15 | Out-Null
        Invoke-RestMethod -Method Post `
            -Uri "http://${Server}:8000/api/stream/fft/start" `
            -ContentType 'application/json' -Body $body -TimeoutSec 15 | Out-Null
        Start-Sleep -Seconds 8
    }
    finally {
        & pktmon stop | Out-Null
        & pktmon filter remove $filterName 2>$null
    }

    & pktmon etl2txt $etl --out $txt | Out-Null
    Write-Host "Capture written to $txt"
}
catch {
    ($_ | Out-String) | Set-Content -LiteralPath `
        (Join-Path $PSScriptRoot 'pktmon_error.txt')
    exit 1
}
