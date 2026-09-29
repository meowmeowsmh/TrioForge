# ============================================================
#  install.ps1 — put the `forge` (and `trioforge`) command on your PATH (Windows).
#
#      powershell -ExecutionPolicy Bypass -File .\install.ps1
#
#  Creates small shims in %USERPROFILE%\bin and adds that folder to your
#  user PATH, so `trioforge` works from any directory. No admin needed.
#
#  Safe to re-run: it only rewrites the shims and the PATH entry.
# ============================================================
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host "TrioForge terminal client"
Write-Host "  repo: $Root"

# ---------------------------------------------------------------- deps
# The terminal client needs these; they are NOT in requirements.txt, which
# only covers the Flask web app.
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $py = "python"
    Write-Host "  deps: no .venv yet — using '$py' from PATH"
}
Write-Host "  deps: installing rich + prompt_toolkit + textual"
& $py -m pip install --quiet --disable-pip-version-check rich prompt_toolkit textual

# ---------------------------------------------------------------- shims
$bin = Join-Path $env:USERPROFILE "bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null

$target = Join-Path $Root "forge.cmd"
foreach ($name in @("forge", "trioforge")) {
    $shim = Join-Path $bin "$name.cmd"
    "@echo off`r`ncall `"$target`" %*" | Set-Content -Path $shim -Encoding ASCII
    Write-Host "  shim:   $shim"
}

# ---------------------------------------------------------------- llama.cpp
# So a local model works with no manual setup. TrioForge also fetches this on
# first use, so this is a convenience, not a requirement.
$llamaDir = Join-Path $Root "tools\llama.cpp"
$have = Get-ChildItem -Path $llamaDir -Filter "llama-server*" -Recurse -File `
    -ErrorAction SilentlyContinue | Select-Object -First 1
if ($env:TRIOFORGE_SKIP_LLAMA -eq "1") {
    Write-Host "  llama:  skipped (TRIOFORGE_SKIP_LLAMA=1)"
} elseif ($have) {
    Write-Host "  llama:  already present under tools\llama.cpp"
} else {
    Write-Host "  llama:  downloading the prebuilt build for this machine (once)..."
    $env:PYTHONPATH = Join-Path $Root "py"
    & $py -c "import llama_installer as L; r = L.install_llamacpp(); print('         ', r['path'] if r['ok'] else 'skipped: ' + r['error'])"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  llama:  could not fetch it now - it will be fetched on first use"
    }
}

# ---------------------------------------------------------------- PATH
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
if ($userPath -split ";" -notcontains $bin) {
    $joined = if ($userPath.TrimEnd(";") -eq "") { $bin } else { "$($userPath.TrimEnd(';'));$bin" }
    [Environment]::SetEnvironmentVariable("Path", $joined, "User")
    Write-Host "  PATH:   added $bin to your user PATH"
    Write-Host
    Write-Host "NOTE: open a NEW terminal for the PATH change to take effect."
} else {
    Write-Host "  PATH:   $bin was already on your PATH"
}

Write-Host
Write-Host "Done. Try:  trioforge --version"
