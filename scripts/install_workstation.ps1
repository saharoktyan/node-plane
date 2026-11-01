# Bootstrap the cargo-dist installer for the newest published Workstation release.
param(
    [ValidateSet('dev', 'beta', 'stable')]
    [string]$Channel = 'stable',
    [string]$Tag = ''
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$headers = @{ 'User-Agent' = 'node-plane-workstation-installer' }
$repo = 'https://api.github.com/repos/saharoktyan/node-plane'
$sourceGuide = 'https://github.com/saharoktyan/node-plane/blob/dev/docs/workstation.md#build-from-source'

if ($Tag -and $Tag -notmatch '^v\d+\.\d+\.\d+(-(alpha|beta|rc)\.\d+)?$') {
    throw 'Invalid release tag.'
}
function Test-ChannelTag([string]$Candidate) {
    $number = '(0|[1-9][0-9]*)'
    $base = "^v$number\.$number\.$number"
    if ($Channel -eq 'dev') { return $Candidate -cmatch "$base-alpha\.$number`$" }
    if ($Channel -eq 'beta') { return $Candidate -cmatch "$base-(beta|rc)\.$number`$" }
    return $Candidate -cmatch "$base`$"
}
function Test-ReleaseMetadata([string]$Candidate) {
    if (-not (Test-ChannelTag $Candidate)) { return $false }
    $uri = "https://github.com/saharoktyan/node-plane/releases/download/$Candidate/RELEASE_METADATA.txt"
    $content = (Invoke-WebRequest -UseBasicParsing -Uri $uri -Headers $headers -TimeoutSec 30).Content
    if ($content.Length -gt 4096) { return $false }
    $fields = @{}
    foreach ($line in ($content.TrimEnd("`r", "`n") -split "`r?`n")) {
        $pair = $line -split '=', 2
        if ($pair.Count -ne 2 -or -not $pair[0] -or $fields.ContainsKey($pair[0])) { return $false }
        $fields[$pair[0]] = $pair[1]
    }
    return ($fields.format -ceq '2' -and $fields.channel -ceq $Channel -and
        $fields.tag -ceq $Candidate -and $fields.version -ceq $Candidate.Substring(1) -and
        $fields.commit -cmatch '^[0-9a-f]{40}$')
}
if (-not $Tag) {
    Write-Host '[1/3] Finding the latest Workstation release...'
    $releases = @()
    for ($page = 1; $page -le 20; $page++) {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$repo/releases?per_page=100&page=$page" -Headers $headers -TimeoutSec 30
        $pageReleases = ConvertFrom-Json -InputObject $response.Content
        $releases += $pageReleases
        if ($pageReleases.Count -lt 100) { break }
    }
    $candidates = @($releases | Where-Object {
        -not $_.draft -and ([bool]$_.prerelease -eq ($Channel -ne 'stable')) -and
        (Test-ChannelTag $_.tag_name) -and
        'node-plane-cli-installer.ps1' -in $_.assets.name -and 'RELEASE_METADATA.txt' -in $_.assets.name
    } | Sort-Object -Descending -Property @(
        @{Expression={ [int](($_.tag_name -split '[v.-]')[1]) }},
        @{Expression={ [int](($_.tag_name -split '[v.-]')[2]) }},
        @{Expression={ [int](($_.tag_name -split '[v.-]')[3]) }},
        @{Expression={ if ($_.tag_name -match '-rc\.') { 2 } elseif ($_.tag_name -match '-beta\.') { 1 } else { 0 } }},
        @{Expression={ [int](($_.tag_name -split '[v.-]')[-1] -replace '[^0-9]', '') }}
    ))
    foreach ($r in $candidates) {
        if (Test-ReleaseMetadata $r.tag_name) { $Tag = $r.tag_name; break }
    }
    if (-not $Tag) { throw "No Windows Workstation release found for this channel. $sourceGuide" }
} elseif (-not (Test-ReleaseMetadata $Tag)) {
    throw 'The release tag lacks matching channel metadata.'
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
    $displayName = if ($Channel -eq 'dev') { 'Node Plane (Dev)' } elseif ($Channel -eq 'beta') { 'Node Plane (Beta)' } else { 'Node Plane' }
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
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $uri -Headers $headers -OutFile $installer
    } catch {
        throw "Could not download the published Workstation installer for $Tag. Check the tag and connection. For source-only tags or the current dev branch: $sourceGuide"
    }
    Write-Host '[3/3] Installing Workstation...'
    $previousConfig = $env:XDG_CONFIG_HOME
    $previousForce = $env:CARGO_DIST_FORCE_INSTALL_DIR
    $previousSpecific = $env:NODE_PLANE_CLI_INSTALL_DIR
    $previousModifyPath = $env:NODE_PLANE_CLI_NO_MODIFY_PATH
    try {
        $env:XDG_CONFIG_HOME = $channelReceiptBase
        $env:CARGO_DIST_FORCE_INSTALL_DIR = $channelInstallDir
        $env:NODE_PLANE_CLI_INSTALL_DIR = $channelInstallDir
        $env:NODE_PLANE_CLI_NO_MODIFY_PATH = '1'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
        if ($LASTEXITCODE -ne 0) { throw "Workstation installer failed (exit $LASTEXITCODE)." }
    } finally {
        $env:XDG_CONFIG_HOME = $previousConfig
        $env:CARGO_DIST_FORCE_INSTALL_DIR = $previousForce
        $env:NODE_PLANE_CLI_INSTALL_DIR = $previousSpecific
        $env:NODE_PLANE_CLI_NO_MODIFY_PATH = $previousModifyPath
    }
    $launcherName = if ($Channel -eq 'stable') { 'node-plane' } else { "node-plane-$Channel" }
    $launcher = Join-Path $channelInstallDir "$launcherName.cmd"
    Set-Content -LiteralPath $launcher -Encoding ASCII -Value '@"%~dp0node-plane.exe" %*'
    # Expose a folder containing only this channel's named launcher. Adding the
    # binary prefix itself would let beta/dev shadow stable's node-plane.exe.
    $launcherDir = Join-Path $channelInstallDir 'launchers'
    New-Item -ItemType Directory -Path $launcherDir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $launcherDir "$launcherName.cmd") -Encoding ASCII -Value '@"%~dp0..\node-plane.exe" %*'
    if ($previousModifyPath -ne '1' -and $env:INSTALLER_NO_MODIFY_PATH -ne '1') {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $entries = @($userPath -split ';' | Where-Object { $_ })
        if (-not ($entries | Where-Object { $_.TrimEnd('\') -ieq $launcherDir.TrimEnd('\') })) {
            [Environment]::SetEnvironmentVariable('Path', (($entries + $launcherDir) -join ';'), 'User')
        }
        if (-not (($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $launcherDir.TrimEnd('\') })) {
            $env:Path = "$launcherDir;$env:Path"
        }
    }
    Install-WorkstationShortcut
    $displayName = if ($Channel -eq 'dev') { 'Node Plane (Dev)' } elseif ($Channel -eq 'beta') { 'Node Plane (Beta)' } else { 'Node Plane' }
    Write-Host "Open $displayName from the Windows Start menu, or run: $launcherName"
} finally {
    Remove-Item -LiteralPath $installer -ErrorAction SilentlyContinue
}
