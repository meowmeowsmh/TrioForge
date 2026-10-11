# ============================================================
#  bootstrap.ps1 - one-line installer for TrioForge (Windows).
#
#      powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/meowmeowsmh/TrioForge/main/bootstrap.ps1 | iex"
#
#  Downloads the repo as a zip (no git), then hands off to TrioForge.bat,
#  which does the rest (Python, venv, deps, llama.cpp). Re-running the same
#  line just launches the already-installed copy - no cd, no git pull.
#
#  $env:TRIOFORGE_DIR = "..."   install somewhere other than ~\TrioForge
#
#  ASCII ONLY, same reason as install.ps1: Windows PowerShell 5.1 reads a
#  BOM-less file as ANSI, and a smart-quote turns into a string delimiter.
# ============================================================
$ErrorActionPreference = "Stop"

$repo    = "https://github.com/meowmeowsmh/TrioForge"
$zipUrl  = "$repo/archive/refs/heads/main.zip"
$dir     = if ($env:TRIOFORGE_DIR) { $env:TRIOFORGE_DIR } else { Join-Path $HOME "TrioForge" }
$bat     = Join-Path $dir "TrioForge.bat"

if (Test-Path $bat) {
    Write-Host "[trio] already installed at $dir - launching"
    Set-Location $dir
    & $bat
    exit $LASTEXITCODE
}

Write-Host "[trio] installing to $dir (no git needed)"
New-Item -ItemType Directory -Path $dir -Force | Out-Null

$tmp = Join-Path $env:TEMP ("trioforge-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
$zip = Join-Path $tmp "trioforge.zip"

try {
    Write-Host "[trio] downloading TrioForge..."
    Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $src = Join-Path $tmp "TrioForge-main"
    Copy-Item -Path (Join-Path $src "*") -Destination $dir -Recurse -Force
}
finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

Set-Location $dir
Write-Host "[trio] starting TrioForge"
& $bat
