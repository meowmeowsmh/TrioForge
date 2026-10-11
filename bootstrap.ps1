# ============================================================
#  bootstrap.ps1 - one-line installer for TrioForge (Windows).
#
#      powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/meowmeowsmh/TrioForge/main/bootstrap.ps1 | iex"
#
#  Clones the repo (once), then hands off to TrioForge.bat, which does the
#  rest (Python, venv, deps, llama.cpp). Requires git. No admin.
#
#  $env:TRIOFORGE_DIR = "..."   install somewhere other than ~\TrioForge
#
#  ASCII ONLY, same reason as install.ps1: Windows PowerShell 5.1 reads a
#  BOM-less file as ANSI, and a smart-quote turns into a string delimiter.
# ============================================================
$ErrorActionPreference = "Stop"

$repo = "https://github.com/meowmeowsmh/TrioForge.git"
$dir  = if ($env:TRIOFORGE_DIR) { $env:TRIOFORGE_DIR } else { Join-Path $HOME "TrioForge" }

if (Test-Path (Join-Path $dir ".git")) {
    Write-Host "[trio] already installed at $dir - updating"
    git -C $dir pull --ff-only --quiet
    if ($LASTEXITCODE -ne 0) { Write-Host "[trio] update skipped (local changes or no network)" }
} else {
    Write-Host "[trio] installing to $dir"
    git clone --depth 1 $repo $dir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[trio] clone failed. Is git installed?" -ForegroundColor Red
        exit 1
    }
}

Set-Location $dir
Write-Host "[trio] starting TrioForge"
& (Join-Path $dir "TrioForge.bat")
