<#
    TrioForge installer for Windows.

    Why this exists: browsers mark downloaded .exe files with "Mark of the Web",
    and Windows shows "Windows protected your PC - unrecognized app" for any exe
    that is not code-signed (TrioForge is not: a certificate costs money every
    year). Downloading from PowerShell instead of a browser leaves no such mark, so
    the warning does not appear - and Unblock-File is applied anyway, as a belt and
    braces for anyone who already downloaded it through a browser.

    Run it with:
        irm https://raw.githubusercontent.com/meowmeowsmh/TrioForge/main/install.ps1 | iex

    Or, if script execution is restricted:
        powershell -ExecutionPolicy Bypass -File install.ps1
#>

[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'TrioForge\application.exe'),
    [switch]$NoLaunch,
    [switch]$Cli
)

$ErrorActionPreference = 'Stop'
$name = if ($Cli) { 'application-cli.exe' } else { 'application.exe' }
$url = "https://github.com/meowmeowsmh/TrioForge/releases/latest/download/$name"

Write-Host ''
Write-Host '  TrioForge installer' -ForegroundColor Yellow
Write-Host '  ------------------'
Write-Host "  Downloading $name ..."

$dir = Split-Path -Parent $Destination
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }

$downloaded = $false
try {
    Invoke-WebRequest -Uri $url -OutFile $Destination -UseBasicParsing
    $downloaded = $true
} catch {
    Write-Host '  PowerShell download failed; trying curl.exe ...' -ForegroundColor DarkYellow
}

if (-not $downloaded) {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) { throw "Could not download $url" }
    & curl.exe -L --fail --silent --show-error -o $Destination $url
    if ($LASTEXITCODE -ne 0) { throw "curl failed with exit code $LASTEXITCODE" }
}

$size = (Get-Item $Destination).Length
Write-Host ("  Saved to {0} ({1:N1} MB)" -f $Destination, ($size / 1MB))

# Belt and braces: if it was marked as "from the internet", remove the mark so
# SmartScreen has nothing to complain about.
try {
    $zone = Get-Item -LiteralPath $Destination -Stream Zone.Identifier -ErrorAction SilentlyContinue
    if ($zone) {
        Unblock-File -LiteralPath $Destination
        Write-Host '  Removed the "downloaded from the internet" mark.'
    }
} catch { }

if ($NoLaunch) {
    Write-Host '  -NoLaunch given: not starting it.' -ForegroundColor DarkGray
    return
}

Write-Host '  Starting TrioForge (a control panel window and the app window appear)...'
Start-Process -FilePath $Destination
Write-Host ''
Write-Host '  Tip: if Windows still asks about the firewall, allow it once.' -ForegroundColor DarkGray
Write-Host '  Nothing is exposed on your network unless you tick "Let other people use this".' -ForegroundColor DarkGray
Write-Host ''
