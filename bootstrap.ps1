# ============================================================
#  bootstrap.ps1 - one-line installer for TrioForge (Windows).
#
#      powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/meowmeowsmh/TrioForge/main/bootstrap.ps1 | iex"
#
#  Downloads the repo as a zip (no git), puts the `forge` / `trioforge`
#  terminal commands on your PATH (so the TUI works from ANY directory), then
#  hands off to TrioForge.bat for the web app (Python, venv, deps, llama.cpp).
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
$bat     = Join-Path $dir "TrioForge.bat"

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

# Put the TUI command (forge) on the PATH, so it works from any directory rather
# than only this folder. `trioforge` is the Linux/macOS alias only: on Windows'
# case-insensitive filesystem the typed name `trioforge` collides with
# TrioForge.bat (web launcher) and TrioForge.exe (its windowed interpreter), so
# it launched the SERVER instead of the TUI. Same shim + PATH step install.ps1
# does, but done here before Python exists: forge.cmd self-installs its own deps
# (rich / textual / duckdb) on first run.
$bin = Join-Path $env:USERPROFILE "bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$target = Join-Path $dir "forge.cmd"
foreach ($name in @("forge")) {
    $shim = Join-Path $bin "$name.cmd"
    "@echo off`r`nset `"FORGE_PROG=%~n0`"`r`ncall `"$target`" %*" |
        Set-Content -Path $shim -Encoding ASCII
    Write-Host "  shim:   $shim"
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
if ($userPath -split ";" -notcontains $bin) {
    $joined = if ($userPath.TrimEnd(";") -eq "") { $bin } else { "$($userPath.TrimEnd(';'));$bin" }
    [Environment]::SetEnvironmentVariable("Path", $joined, "User")
    Write-Host "[trio] added $bin to your user PATH (open a NEW terminal to use 'trioforge')"
} else {
    Write-Host "[trio] $bin is already on your PATH"
}

Set-Location $dir
Write-Host "[trio] starting TrioForge"
& $bat
