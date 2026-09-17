# llama_installer.py – auto-download + install llama.cpp (llama-server) for the
# detected platform / GPU backend, so macOS/Linux/Windows users never have to
# hand-build or hand-place it.
#
# llama.cpp publishes prebuilt binaries under its "bXXXX" (nightly) releases,
# one asset per OS + backend. We pick the asset matching the detected backend
# (Metal/CUDA/ROCm/Vulkan/CPU), download it, extract it under tools/llama.cpp,
# and surface the llama-server path so the app can auto-start it.
#
# Everything is explicit-click (never runs on its own) and returns a clear
# error on any failure. Uses only stdlib (urllib / tarfile / zipfile / hashlib).

import os
import platform
import shutil
import tarfile
import urllib.request
import zipfile

from paths import root_path

# Where extracted binaries land: <project>/tools/llama.cpp/<release-tag>/...
INSTALL_ROOT = root_path("tools", "llama.cpp")
RELEASES_URL = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=5"
DOWNLOAD_BASE = "https://github.com/ggml-org/llama.cpp/releases/download"


def _gpu_backend():
    """Detect backend without importing setup_check (keep this module standalone)."""
    import setup_check as sc
    return sc._gpu_backend()


def _platform_key():
    os_name = platform.system()
    arch = (platform.machine() or "").lower()
    x = "arm64" if arch in ("arm64", "aarch64") else "x64"
    return os_name, x


def _fetch_releases():
    """Return (tag, [asset names], {asset: download_url}) for the newest b* release."""
    import json as _json
    req = urllib.request.Request(RELEASES_URL, headers={"User-Agent": "TrioForge"})
    with urllib.request.urlopen(req, timeout=25) as r:
        releases = _json.load(r)
    for rel in releases:
        tag = rel.get("tag_name", "")
        assets = rel.get("assets") or []
        if tag.startswith("b") and assets:
            names = [a["name"] for a in assets]
            urls = {a["name"]: a.get("browser_download_url") for a in assets}
            return tag, names, urls
    return None


def _pick_asset(names, backend):
    """Pick the prebuilt asset best matching OS + arch + backend. Returns name or None.

    What each platform gets:
      macOS            - the macos build (Metal accelerated on Apple Silicon)
      Windows + NVIDIA - the CUDA build, CPU build if no CUDA asset is published
      Windows + AMD    - the ROCm build, else Vulkan, else CPU
      Linux + AMD      - the ROCm build, else CPU
      Linux + NVIDIA   - the VULKAN build: llama.cpp publishes no prebuilt CUDA for
                         Linux, and Vulkan drives NVIDIA fine through its driver
      anything else    - the CPU build

    The backend-specific match is a PREFIX match, not a fixed version list: llama.cpp
    renames these every few weeks (cuda-12.4, cuda-13.3, cuda-13.4, ...), and hard-coding
    the versions meant a rename would silently drop the user to a CPU build - the
    slowest possible outcome nobody asked for. The highest-sorting matching name wins.
    """
    os_name, x = _platform_key()
    b = backend if backend in ("cuda", "rocm", "vulkan") else "cpu"

    def find(*subs):
        for sub in subs:
            for n in names:
                if n.endswith(sub):
                    return n
        return None

    def find_prefix(*prefixes):
        """Newest asset whose name contains one of these prefixes (e.g. cuda-13.4)."""
        for pre in prefixes:
            hits = sorted(n for n in names if pre in n and n.endswith((".zip", ".tar.gz")))
            if hits:
                return hits[-1]
        return None

    if os_name == "Darwin":
        return find("-bin-macos-{}.tar.gz".format(x)) or find("-bin-macos-arm64.tar.gz", "-bin-macos-x64.tar.gz")
    if os_name == "Windows":
        if b == "cuda":
            return (find("-bin-win-cuda-12.4-{}.zip".format(x)) or
                    find_prefix("-bin-win-cuda-") or
                    find("-bin-win-cpu-{}.zip".format(x)))
        if b == "rocm":
            return (find("-bin-win-rocm-10.0-x64.zip") or find_prefix("-bin-win-rocm-") or
                    find("-bin-win-vulkan-{}.zip".format(x)) or find("-bin-win-cpu-{}.zip".format(x)))
        if b == "vulkan":
            return find("-bin-win-vulkan-{}.zip".format(x)) or find("-bin-win-cpu-{}.zip".format(x))
        return find("-bin-win-cpu-{}.zip".format(x))
    # Linux
    if b == "rocm":
        return (find("-bin-ubuntu-rocm-10.0-{}.tar.gz".format(x)) or find_prefix("-bin-ubuntu-rocm-") or
                find("-bin-ubuntu-{}.tar.gz".format(x)))
    if b in ("cuda", "vulkan"):
        return (find("-bin-ubuntu-vulkan-{}.tar.gz".format(x)) or find_prefix("-bin-ubuntu-vulkan-") or
                find("-bin-ubuntu-{}.tar.gz".format(x)))
    return find("-bin-ubuntu-{}.tar.gz".format(x))


def _find_llama_server(root):
    """Return the llama-server executable path under `root`, or None."""
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f in ("llama-server", "llama-server.exe"):
                return os.path.join(dirpath, f)
    return None


def find_installed():
    """Return the llama-server path already installed under tools/llama.cpp, or None."""
    if not os.path.isdir(INSTALL_ROOT):
        return None
    # Latest extraction dir first (tags sort reverse by version-ish).
    releases = [d for d in os.listdir(INSTALL_ROOT) if os.path.isdir(os.path.join(INSTALL_ROOT, d))]
    for rel in sorted(releases, reverse=True):
        p = _find_llama_server(os.path.join(INSTALL_ROOT, rel))
        if p:
            return p
    return None


def install_llamacpp(backend=None):
    """Download + extract the llama.cpp build for the detected backend.

    Returns {"ok": bool, "path": <llama-server abs path> | "", "error": <str> | ""}.
    """
    if backend is None:
        backend = _gpu_backend().get("backend", "cpu")

    info = _fetch_releases()
    if not info:
        return {"ok": False, "path": "", "error": "Could not reach llama.cpp GitHub releases (offline? rate-limited?)."}
    tag, names, urls = info

    asset = _pick_asset(names, backend)
    if not asset:
        return {"ok": False, "path": "",
                "error": "No prebuilt llama.cpp binary for this platform ({}/{}/{}) — "
                         "build it yourself or use the CPU/Vulkan build.".format(
                             platform.system(), backend, (platform.machine() or ""))}
    url = urls.get(asset)
    if not url:
        return {"ok": False, "path": "", "error": "No download URL for {}".format(asset)}

    try:
        os.makedirs(INSTALL_ROOT, exist_ok=True)
        archive = os.path.join(INSTALL_ROOT, asset)
        # Stream-download (large tarballs) to a temp file first.
        tmp = archive + ".part"
        _download(url, tmp)
        os.replace(tmp, archive)

        extract_dir = os.path.join(INSTALL_ROOT, tag)
        if os.path.isdir(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        os.makedirs(extract_dir, exist_ok=True)
        if asset.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract_dir)
        elif asset.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive) as tf:
                tf.extractall(extract_dir, filter="data")
        else:
            return {"ok": False, "path": "", "error": "Unsupported archive type: {}".format(asset)}

        server = _find_llama_server(extract_dir)
        if not server:
            return {"ok": False, "path": "", "error": "llama-server not found inside the downloaded build."}
        if os.name != "nt":
            os.chmod(server, 0o755)
        return {"ok": True, "path": server, "error": ""}
    except Exception as e:
        return {"ok": False, "path": "", "error": "Install failed: {}".format(e)}


def _download(url, dest):
    """Stream-download `url` to `dest` (progress-aware, resumable by overwriting)."""
    req = urllib.request.Request(url, headers={"User-Agent": "TrioForge"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)  # 1 MB
            if not chunk:
                break
            f.write(chunk)


if __name__ == "__main__":
    import json as _json
    gpu = _gpu_backend()
    print("Detected backend:", gpu)
    print("Already installed:", find_installed())
    print(_json.dumps(install_llamacpp(), indent=2))
