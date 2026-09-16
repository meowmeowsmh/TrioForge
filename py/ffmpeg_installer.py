# ffmpeg_installer.py – auto-download a MODERN ffmpeg, so audio/video features do
# not depend on whatever old build happens to be on the user's PATH.
#
# Why this exists: a very old ffmpeg (e.g. a 2013 build shipped inside some Python
# package, which is what a real user had) silently fails on modern containers and
# codecs, and every audio/video feature then reports a confusing error. TrioForge
# prefers this managed install over anything found on PATH.
#
# Everything is explicit-click (never runs on its own), stdlib only.

import os
import platform
import shutil
import tarfile
import urllib.request
import zipfile

from paths import root_path


def _no_window():
    """CREATE_NO_WINDOW for console programs (see video_to_text._hidden_flags).

    The app runs under pythonw, so a console program started without this makes
    Windows allocate a NEW console window for it - the "cmd keeps popping up" bug.
    """
    import subprocess as _sp
    import os as _os
    if _os.name != "nt":
        return 0
    return getattr(_sp, "CREATE_NO_WINDOW", 0)



# Where extracted binaries land: <project>/tools/ffmpeg/<tag>/...
INSTALL_ROOT = root_path("tools", "ffmpeg")
RELEASES_URL = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"

_FFMPEG_NAMES = ("ffmpeg", "ffmpeg.exe")


def _platform_key():
    arch = (platform.machine() or "").lower()
    if arch in ("arm64", "aarch64"):
        return "arm64"
    return "x64"


def _fetch_release():
    """Return (tag, [asset names], {asset: url}) for the latest BtbN build."""
    import json as _json
    req = urllib.request.Request(RELEASES_URL, headers={"User-Agent": "TrioForge"})
    with urllib.request.urlopen(req, timeout=25) as r:
        rel = _json.load(r)
    assets = rel.get("assets") or []
    if not assets:
        return None
    names = [a["name"] for a in assets]
    urls = {a["name"]: a.get("browser_download_url") for a in assets}
    return rel.get("tag_name", "latest"), names, urls


def _pick_asset(names):
    """Pick the static GPL build for this OS/arch. Returns the asset name or None."""
    os_name = platform.system()
    x = _platform_key()

    def find(*subs):
        for sub in subs:
            for n in names:
                if n.endswith(sub):
                    return n
        return None

    if os_name == "Windows":
        if x == "arm64":
            return find("-winarm64-gpl.zip") or find("-win64-gpl.zip")
        return find("-win64-gpl.zip")
    if os_name == "Darwin":
        return find("-macos64-gpl.tar.xz")
    return find("-linux64-gpl.tar.xz" if x == "x64" else "-linuxarm64-gpl.tar.xz")


def _find_ffmpeg(root):
    """Return the ffmpeg executable path under `root`, or None."""
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f in _FFMPEG_NAMES:
                return os.path.join(dirpath, f)
    return None


def find_installed():
    """The ffmpeg this app installed under tools/ffmpeg, or None."""
    if not os.path.isdir(INSTALL_ROOT):
        return None
    for rel in sorted(os.listdir(INSTALL_ROOT), reverse=True):
        d = os.path.join(INSTALL_ROOT, rel)
        if os.path.isdir(d):
            p = _find_ffmpeg(d)
            if p:
                return p
    return None


def version_of(path):
    """The `ffmpeg -version` first line, or "" (used to judge how old a build is)."""
    import subprocess
    try:
        r = subprocess.run([path, "-version"], capture_output=True, timeout=20,
                           creationflags=_no_window())
        line = (r.stdout or b"").decode("utf-8", "replace").splitlines()
        return line[0].strip() if line else ""
    except Exception:
        return ""


def install_ffmpeg():
    """Download + extract a current ffmpeg.

    Returns {"ok": bool, "path": <ffmpeg abs path> | "", "error": <str> | ""}.
    """
    try:
        info = _fetch_release()
    except Exception as e:
        return {"ok": False, "path": "", "error": "Could not reach the FFmpeg releases ({})".format(e)}
    if not info:
        return {"ok": False, "path": "", "error": "No downloadable FFmpeg build found."}
    tag, names, urls = info

    asset = _pick_asset(names)
    if not asset:
        return {"ok": False, "path": "",
                "error": "No prebuilt FFmpeg for this platform ({}/{}).".format(
                    platform.system(), platform.machine())}
    url = urls.get(asset)
    if not url:
        return {"ok": False, "path": "", "error": "No download URL for {}".format(asset)}

    try:
        os.makedirs(INSTALL_ROOT, exist_ok=True)
        archive = os.path.join(INSTALL_ROOT, asset)
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
        elif asset.endswith((".tar.xz", ".tar.gz", ".tgz")):
            with tarfile.open(archive) as tf:
                tf.extractall(extract_dir, filter="data")
        else:
            return {"ok": False, "path": "", "error": "Unsupported archive: {}".format(asset)}

        exe = _find_ffmpeg(extract_dir)
        if not exe:
            return {"ok": False, "path": "", "error": "ffmpeg not found inside the downloaded build."}
        if os.name != "nt":
            os.chmod(exe, 0o755)
        # The archive is ~190 MB and no longer needed once extracted.
        try:
            os.remove(archive)
        except Exception:
            pass
        # TrioForge only ever runs `ffmpeg`, and each binary in this build is ~160 MB
        # statically linked - so drop the player, the prober and the docs (~330 MB).
        bin_dir = os.path.dirname(exe)
        for junk in ("ffplay", "ffplay.exe", "ffprobe", "ffprobe.exe"):
            try:
                p = os.path.join(bin_dir, junk)
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
        shutil.rmtree(os.path.join(os.path.dirname(bin_dir), "doc"), ignore_errors=True)
        return {"ok": True, "path": exe, "error": ""}
    except Exception as e:
        return {"ok": False, "path": "", "error": "Install failed: {}".format(e)}


def _download(url, dest):
    """Stream-download `url` to `dest` (1 MB chunks; these archives are ~100 MB)."""
    req = urllib.request.Request(url, headers={"User-Agent": "TrioForge"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


if __name__ == "__main__":
    import json as _json
    print("Already installed:", find_installed())
    result = install_ffmpeg()
    print(_json.dumps(result, indent=2))
    if result.get("path"):
        print(version_of(result["path"]))
