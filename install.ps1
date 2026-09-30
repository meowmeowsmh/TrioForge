# ============================================================
#  install.ps1 - put the `forge` (and `trioforge`) command on your PATH (Windows).
#
#      powershell -ExecutionPolicy Bypass -File .\install.ps1
#
#  Creates small shims in %USERPROFILE%\bin and adds that folder to your
#  user PATH, so `trioforge` works from any directory. No admin needed.
#
#  Safe to re-run: it only rewrites the shims and the PATH entry.
#
#  ASCII ONLY, and that is load-bearing: this file is UTF-8 with no BOM, and
#  Windows PowerShell 5.1 reads a BOM-less file as ANSI. An em-dash becomes the
#  three bytes a-euro-smartquote, and PowerShell treats that SMART QUOTE as a
#  string delimiter - so one dash inside a Write-Host string ends the string
#  early, the rest of the line parses as code, and the whole script fails with 11
#  syntax errors before running a single command. That is exactly what happened
#  here: this installer could not run at all. Keep it ASCII.
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
    Write-Host "  deps: no .venv yet - using '$py' from PATH"
}
# A venv made by uv - which uv.lock implies, and which this repo's own .venv is -
# contains NO pip: `python -m pip` dies with "No module named pip". That is a
# native command's non-zero exit, so $ErrorActionPreference = "Stop" does not stop
# it, so the old version created the shims anyway and printed "Done", leaving
# `trioforge` to fail later with ModuleNotFoundError: textual. Install with
# whatever this interpreter really has, prove the imports work, and refuse to
# create a launcher that cannot run.
$deps = @("rich", "prompt_toolkit", "textual")

function Test-TuiDeps([string]$interpreter) {
    & $interpreter -c "import rich, prompt_toolkit, textual" 2>$null
    return ($LASTEXITCODE -eq 0)
}

if (Test-TuiDeps $py) {
    Write-Host "  deps: already present (rich + prompt_toolkit + textual)"
} else {
    $ok = $false

    & $py -m pip --version *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  deps: pip install rich + prompt_toolkit + textual"
        & $py -m pip install --quiet --disable-pip-version-check @deps
        $ok = Test-TuiDeps $py
    } else {
        Write-Host "  deps: this interpreter has no pip (uv venvs ship without one)"
    }

    if (-not $ok -and (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "  deps: uv pip install rich + prompt_toolkit + textual"
        & uv pip install --python $py --quiet @deps
        $ok = Test-TuiDeps $py
    }

    if (-not $ok) {
        Write-Host "  deps: bootstrapping pip with ensurepip, then installing"
        & $py -m ensurepip --upgrade *> $null
        & $py -m pip install --quiet --disable-pip-version-check @deps
        $ok = Test-TuiDeps $py
    }

    if (-not $ok) {
        Write-Host ""
        Write-Host "ERROR: could not install rich / prompt_toolkit / textual into:" -ForegroundColor Red
        Write-Host "       $py" -ForegroundColor Red
        Write-Host ""
        Write-Host "  No launcher was created, on purpose: a 'trioforge' that exists and then"
        Write-Host "  dies with ModuleNotFoundError: textual is worse than no launcher at all."
        Write-Host ""
        Write-Host "  Install them by hand, then re-run this script:"
        Write-Host "      uv pip install --python `"$py`" rich prompt_toolkit textual"
        exit 1
    }
    Write-Host "  deps: installed"
}

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
