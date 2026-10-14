# Bootstrap the cargo-dist installer for the newest published Workstation release.
param(
    [ValidateSet('dev', 'stable')]
    [string]$Channel = 'stable',
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
    # Windows PowerShell 5.1 can emit the entire REST array as one pipeline
    # object. Parse the response explicitly before iterating releases.
    $response = Invoke-WebRequest -UseBasicParsing -Uri "$repo/releases?per_page=100" -Headers $headers
    $releases = ConvertFrom-Json -InputObject $response.Content
    $release = $null
    foreach ($r in $releases) {
        if ($r.draft) { continue }
        if (($Channel -eq 'dev') -ne [bool]$r.prerelease) { continue }
        $hasInstaller = $false
        if ($null -ne $r.assets) {
            foreach ($a in $r.assets) {
                if ($a.name -eq 'node-plane-cli-installer.ps1') {
                    $hasInstaller = $true
                    break
                }
            }
        }
        if ($hasInstaller) {
            $release = $r
            break
        }
    }
    if (-not $release) { throw 'No Windows Workstation release found for this channel.' }
    $Tag = $release.tag_name
}

if (($Channel -eq 'dev') -ne ($Tag -match '-alpha\.')) {
    throw 'The release tag must match the requested Workstation channel.'
}
$channelInstallDir = Join-Path $env:USERPROFILE ".local\share\node-plane-workstation\$Channel"
if ($env:CARGO_DIST_INSTALL_DIR) { $channelInstallDir = $env:CARGO_DIST_INSTALL_DIR }
if ($env:CARGO_DIST_FORCE_INSTALL_DIR) { $channelInstallDir = $env:CARGO_DIST_FORCE_INSTALL_DIR }
if ($env:NODE_PLANE_CLI_INSTALL_DIR) { $channelInstallDir = $env:NODE_PLANE_CLI_INSTALL_DIR }
$receiptBase = if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { $env:LOCALAPPDATA }
$channelReceiptBase = Join-Path $receiptBase "node-plane-workstation\$Channel"

function Install-WorkstationShortcut {
    # Use the stable cargo-dist install path, rather than PATH in this process:
    # the installer updates PATH for future shells only.
    $installDir = $channelInstallDir
    $executable = Join-Path $installDir 'node-plane.exe'
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "Installed Workstation executable not found: $executable"
    }
    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    New-Item -ItemType Directory -Path $programs -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $displayName = if ($Channel -eq 'dev') { 'Node Plane (Dev)' } else { 'Node Plane' }
    $shortcut = $shell.CreateShortcut((Join-Path $programs "$displayName.lnk"))
    $terminal = Get-Command wt.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($terminal) {
        $shortcut.TargetPath = $terminal.Source
        $shortcut.Arguments = '-w new new-tab --title "{0}" -d "{1}" "{2}"' -f $displayName, $env:USERPROFILE, $executable
    } else {
        # Console applications open in Windows' configured terminal host.
        $shortcut.TargetPath = $executable
        $shortcut.Arguments = ''
    }
    $shortcut.WorkingDirectory = $env:USERPROFILE
    $shortcut.Description = $displayName
    $shortcut.IconLocation = "$executable,0"
    $shortcut.Save()
}

$installer = Join-Path ([IO.Path]::GetTempPath()) "node-plane-installer-$([guid]::NewGuid()).ps1"
try {
    Write-Host "[2/3] Downloading the release installer for $Tag..."
    $uri = "https://github.com/saharoktyan/node-plane/releases/download/$Tag/node-plane-cli-installer.ps1"
    Invoke-WebRequest -UseBasicParsing -Uri $uri -Headers $headers -OutFile $installer
    Write-Host '[3/3] Installing Workstation...'
    $previousConfig = $env:XDG_CONFIG_HOME
    $previousForce = $env:CARGO_DIST_FORCE_INSTALL_DIR
    $previousSpecific = $env:NODE_PLANE_CLI_INSTALL_DIR
    try {
        $env:XDG_CONFIG_HOME = $channelReceiptBase
        $env:CARGO_DIST_FORCE_INSTALL_DIR = $channelInstallDir
        $env:NODE_PLANE_CLI_INSTALL_DIR = $channelInstallDir
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
        if ($LASTEXITCODE -ne 0) { throw "Workstation installer failed (exit $LASTEXITCODE)." }
    } finally {
        $env:XDG_CONFIG_HOME = $previousConfig
        $env:CARGO_DIST_FORCE_INSTALL_DIR = $previousForce
        $env:NODE_PLANE_CLI_INSTALL_DIR = $previousSpecific
    }
    $launcherName = if ($Channel -eq 'dev') { 'node-plane-dev' } else { 'node-plane' }
    $launcher = Join-Path $channelInstallDir "$launcherName.cmd"
    Set-Content -LiteralPath $launcher -Encoding ASCII -Value '@"%~dp0node-plane.exe" %*'
    Install-WorkstationShortcut
    $displayName = if ($Channel -eq 'dev') { 'Node Plane (Dev)' } else { 'Node Plane' }
    Write-Host "Open $displayName from the Windows Start menu, or run: $launcherName"
} finally {
    Remove-Item -LiteralPath $installer -ErrorAction SilentlyContinue
}
