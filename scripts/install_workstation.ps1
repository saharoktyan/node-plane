# Bootstrap the cargo-dist installer for the newest published Workstation release.
param(
    [ValidateSet('dev', 'stable')]
    [string]$Channel = 'dev',
    [string]$Tag = ''
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$headers = @{ 'User-Agent' = 'node-plane-workstation-installer' }
$repo = 'https://api.github.com/repos/saharoktyan/node-plane'

if ($Tag -and $Tag -notmatch '^v\d+\.\d+\.\d+(-alpha\.\d+)?$') {
    throw 'Invalid release tag.'
}
if (-not $Tag) {
    Write-Host '[1/3] Finding the latest Workstation release...'
    $releases = @(Invoke-RestMethod -Uri "$repo/releases?per_page=100" -Headers $headers)
    $release = $releases | Where-Object {
        -not $_.draft -and ($Channel -eq 'dev' -or -not $_.prerelease) -and
        ($_.assets.name -contains 'node-plane-cli-installer.ps1')
    } | Select-Object -First 1
    if (-not $release) { throw 'No Windows Workstation release found for this channel.' }
    $Tag = $release.tag_name
}

$installer = Join-Path ([IO.Path]::GetTempPath()) "node-plane-installer-$([guid]::NewGuid()).ps1"
try {
    Write-Host "[2/3] Downloading the release installer for $Tag..."
    $uri = "https://github.com/saharoktyan/node-plane/releases/download/$Tag/node-plane-cli-installer.ps1"
    Invoke-WebRequest -UseBasicParsing -Uri $uri -Headers $headers -OutFile $installer
    Write-Host '[3/3] Installing Workstation...'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
    if ($LASTEXITCODE -ne 0) { throw "Workstation installer failed (exit $LASTEXITCODE)." }
    Write-Host 'Open a new terminal, then run: node-plane'
} finally {
    Remove-Item -LiteralPath $installer -ErrorAction SilentlyContinue
}
