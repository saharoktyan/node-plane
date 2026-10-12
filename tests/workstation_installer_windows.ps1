# Run with Windows PowerShell 5.1 as well as PowerShell 7. No network; files stay in a temporary directory.
$ErrorActionPreference = 'Stop'
$downloads = New-Object 'System.Collections.Generic.List[string]'
function Invoke-WebRequest {
    param($Uri, $Headers, $OutFile, [switch]$UseBasicParsing)
    if ($OutFile) {
        $downloads.Add($Uri)
        Set-Content -LiteralPath $OutFile -Value '# mock installer'
        return
    }
    return [pscustomobject]@{ Content = '[{"tag_name":"v0.4.3-alpha.67","draft":false,"prerelease":true,"assets":[]},{"tag_name":"v0.4.3-alpha.66","draft":false,"prerelease":true,"assets":[{"name":"node-plane-cli-installer.ps1"}]},{"tag_name":"v0.4.3","draft":false,"prerelease":false,"assets":[{"name":"node-plane-cli-installer.ps1"}]}]' }
}
function powershell.exe { $global:LASTEXITCODE = 0 }
function Get-Command {
    param($Name, $CommandType, $ErrorAction)
    if ($Name -eq 'wt.exe') { return $testTerminal }
    throw "Unexpected command discovery: $Name"
}
$installer = Join-Path $PSScriptRoot '../scripts/install_workstation.ps1'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) "node-plane-shortcut-test-$([guid]::NewGuid())"
$oldAppData = $env:APPDATA
$oldUserProfile = $env:USERPROFILE
$oldInstallDir = $env:CARGO_DIST_INSTALL_DIR
$oldForceInstallDir = $env:CARGO_DIST_FORCE_INSTALL_DIR
try {
    $env:APPDATA = Join-Path $testRoot 'Roaming'
    $env:USERPROFILE = Join-Path $testRoot 'User with spaces'
    $env:CARGO_DIST_INSTALL_DIR = ''
    $env:CARGO_DIST_FORCE_INSTALL_DIR = ''
    $bin = Join-Path $env:USERPROFILE '.local\bin'
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    $executable = Join-Path $bin 'node-plane.exe'
    Set-Content -LiteralPath $executable -Value 'mock executable'
    $terminalPath = Join-Path $testRoot 'wt.exe'
    Set-Content -LiteralPath $terminalPath -Value 'mock terminal'
    $testTerminal = [pscustomobject]@{ Source = $terminalPath }
    & $installer -Channel dev
    if ($downloads[$downloads.Count - 1] -notlike '*/v0.4.3-alpha.66/node-plane-cli-installer.ps1') {
        throw "Wrong development release: $($downloads[$downloads.Count - 1])"
    }
    $linkPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Node Plane Workstation.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($linkPath)
    if ($link.TargetPath -ne $terminalPath -or $link.Arguments -notlike ('*"' + $executable + '"*')) {
        throw 'Shortcut does not launch Workstation through Windows Terminal.'
    }
    $testTerminal = $null
    & $installer -Channel stable
    if ($downloads[$downloads.Count - 1] -notlike '*/v0.4.3/node-plane-cli-installer.ps1') {
        throw "Wrong stable release: $($downloads[$downloads.Count - 1])"
    }
    $linkPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Node Plane Workstation.lnk'
    if (-not (Test-Path -LiteralPath $linkPath)) { throw 'Start menu shortcut missing.' }
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($linkPath)
    if ($link.WorkingDirectory -ne $env:USERPROFILE) { throw 'Incorrect shortcut working directory.' }
    if ($link.TargetPath -ne $executable -or $link.Arguments) {
        throw 'Shortcut does not launch the installed executable.'
    }
    # Reinstalling updates the same shortcut instead of adding duplicates.
    $links = @(Get-ChildItem -LiteralPath (Split-Path $linkPath) -Filter '*.lnk')
    if ($links.Count -ne 1) { throw 'Duplicate shortcuts created.' }
} finally {
    $env:APPDATA = $oldAppData
    $env:USERPROFILE = $oldUserProfile
    $env:CARGO_DIST_INSTALL_DIR = $oldInstallDir
    $env:CARGO_DIST_FORCE_INSTALL_DIR = $oldForceInstallDir
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}
