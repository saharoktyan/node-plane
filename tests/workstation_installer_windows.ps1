# Run with Windows PowerShell 5.1 as well as PowerShell 7. No network or install.
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
$installer = Join-Path $PSScriptRoot '../scripts/install_workstation.ps1'
& $installer -Channel dev
if ($downloads[$downloads.Count - 1] -notlike '*/v0.4.3-alpha.66/node-plane-cli-installer.ps1') {
    throw "Wrong development release: $($downloads[$downloads.Count - 1])"
}
& $installer -Channel stable
if ($downloads[$downloads.Count - 1] -notlike '*/v0.4.3/node-plane-cli-installer.ps1') {
    throw "Wrong stable release: $($downloads[$downloads.Count - 1])"
}
