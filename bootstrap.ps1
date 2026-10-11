# ============================================================
#  bootstrap.ps1 - one-line installer for TrioForge (Windows).
#
#      powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/meowmeowsmh/TrioForge/main/bootstrap.ps1 | iex"
#
#  Downloads the repo as a zip (no git), puts `forge` / `trioforge` (TUI) and
#  `trioforge-web` (web app) on your PATH (works from ANY directory), then
#  hands off to TrioForge-Web.bat for the web app (Python, venv, deps, llama.cpp).
#  Re-running the same line just launches the already-installed copy.
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
$bat     = Join-Path $dir "TrioForge-Web.bat"

if (Test-Path $bat) {
    Write-Host "[trio] already installed at $dir"
} else {
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
}

# Put the TUI commands (forge + trioforge) and the web app (trioforge-web) on
# the PATH, so they work from any directory rather than only this folder. The
# web launcher lives at TrioForge-Web.bat so the typed `trioforge` no longer
# collides with it on the case-insensitive filesystem. Done here before Python
# exists: forge.cmd self-installs its own deps (rich / textual / duckdb) on
# first run.
$bin = Join-Path $env:USERPROFILE "bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null

$tuiTarget = Join-Path $dir "forge.cmd"
foreach ($name in @("forge", "trioforge")) {
    $shim = Join-Path $bin "$name.cmd"
    "@echo off`r`nset `"FORGE_PROG=%~n0`"`r`ncall `"$tuiTarget`" %*" |
        Set-Content -Path $shim -Encoding ASCII
    Write-Host "  shim:   $shim"
}

$webTarget = Join-Path $dir "TrioForge-Web.bat"
$webShim = Join-Path $bin "trioforge-web.cmd"
"@echo off`r`nset `"TRIOFORGE_PROG=trioforge-web`"`r`ncall `"$webTarget`" %*" | Set-Content -Path $webShim -Encoding ASCII
Write-Host "  shim:   $webShim"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
if ($userPath -split ";" -notcontains $bin) {
    $joined = if ($userPath.TrimEnd(";") -eq "") { $bin } else { "$($userPath.TrimEnd(';'));$bin" }
    [Environment]::SetEnvironmentVariable("Path", $joined, "User")
    Write-Host "[trio] added $bin to your user PATH (open a NEW terminal to use 'forge')"
} else {
    Write-Host "[trio] $bin is already on your PATH"
}

Set-Location $dir
Write-Host "[trio] starting TrioForge"
& $bat
