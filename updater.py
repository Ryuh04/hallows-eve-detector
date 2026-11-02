"""
Updates from GitHub: is there a newer version of the app, and installing it.

Two kinds of copies update differently:

* The exe (HallowsEveDetector.exe, built by GitHub on every push): its version is
  the release tag baked into it at build time. Updating downloads the newest
  release's exe, swaps it in for the running one (Windows lets a running exe be
  renamed) and restarts.
* The Python files (a clone or a ZIP of the repository): versions are commits.
  The commit a copy was installed from is kept in installed_version.json (or read
  from .git in a clone). Updating downloads that commit's files and copies them
  over the app's own, then installs new packages if requirements.txt changed.

Either way, your settings, logs and Discord config are never touched.

Run on its own to check from the command line:
    python updater.py           # say whether an update is available
    python updater.py --install # and install it
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = "Ryuh04/hallows-eve-detector"  # GitHub "owner/name"
BRANCH = "main"
API = "https://api.github.com"
EXE_NAME = "HallowsEveDetector.exe"  # the release asset
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # where an exe's bundled files unpack
STATE_PATH = APP_DIR / "installed_version.json"
# Files that belong to the person using the app, never replaced by an update (they aren't
# in the repository anyway; this is a second line of defence).
KEEP = {"discord_config.json", "private_servers.json", "window_names.json", "rebirth_log.csv",
        "ui_settings.json", "hotbar_slots.json", "auto_rebirth.json", "hidden_windows.json",
        "installed_version.json", "detector_output.log"}
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: run pip without flashing a console


class UpdateError(Exception):
    pass


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "hallows-eve-detector-updater",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise UpdateError("GitHub is limiting checks right now; try again in a while") from None
        if e.code == 404:
            raise UpdateError(f"couldn't find {'a release of ' if FROZEN else ''}{REPO} on GitHub") from None
        raise UpdateError(f"GitHub answered HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError(f"couldn't reach GitHub ({getattr(e, 'reason', e)})") from None


def installed(app_dir=APP_DIR):
    """{'sha', 'date', 'tag'} of the version this copy is, or None if it isn't known (e.g.
    the files were downloaded as a ZIP by hand)."""
    if FROZEN:
        try:
            info = json.loads((BUNDLE_DIR / "build_info.json").read_text())
            return {"sha": info.get("sha", ""), "date": info.get("date", ""), "tag": info["version"]}
        except (OSError, ValueError, KeyError):
            return None
    git = app_dir / ".git"
    if git.is_dir():
        try:
            head = (git / "HEAD").read_text().strip()
            if head.startswith("ref: "):
                ref = git / head[5:]
                if ref.exists():
                    return {"sha": ref.read_text().strip(), "date": ""}
                packed = (git / "packed-refs").read_text().splitlines()
                for line in packed:
                    if line.endswith(" " + head[5:]):
                        return {"sha": line.split()[0], "date": ""}
            else:
                return {"sha": head, "date": ""}
        except OSError:
            pass
    try:
        state = json.loads((app_dir / STATE_PATH.name).read_text())
        return {"sha": state["sha"], "date": state.get("date", "")}
    except (OSError, ValueError, KeyError):
        return None


def latest():
    """The newest version on GitHub: the latest release for the exe ({'tag', 'sha', 'date',
    'message', 'url'}), the newest commit for the Python files ({'sha', 'date', 'message'})."""
    if FROZEN:
        data = json.loads(_get(f"{API}/repos/{REPO}/releases/latest"))
        asset = next((a for a in data.get("assets", []) if a["name"] == EXE_NAME), None)
        if asset is None:
            raise UpdateError("the newest release has no exe to download")
        message = (data.get("body") or data.get("name") or data["tag_name"]).strip().splitlines()[0]
        return {"tag": data["tag_name"], "sha": data.get("target_commitish", ""),
                "date": (data.get("published_at") or "")[:10], "message": message,
                "url": asset["browser_download_url"]}
    data = json.loads(_get(f"{API}/repos/{REPO}/commits/{BRANCH}"))
    commit = data["commit"]
    return {"sha": data["sha"], "date": commit["committer"]["date"][:10],
            "message": commit["message"].splitlines()[0]}


def check(app_dir=APP_DIR):
    """(state, mine, newest): state is 'up_to_date', 'available' or 'unknown' (this copy's
    version isn't known, so installing the newest is the way to be sure)."""
    mine, newest = installed(app_dir), latest()
    if mine is None:
        return "unknown", None, newest
    same = mine.get("tag") == newest["tag"] if FROZEN else mine["sha"] == newest["sha"]
    return ("up_to_date" if same else "available"), mine, newest


def short(sha):
    return (sha or "")[:7]


def display(version):
    """How a version reads in the app: the release tag for the exe, the short commit otherwise."""
    if not version:
        return "—"
    return version.get("tag") or short(version.get("sha"))


def install(newest, app_dir=APP_DIR, progress=print):
    """Bring this copy to `newest` (from latest()). Returns a summary line."""
    if FROZEN:
        return _install_exe(newest, progress)
    old_requirements = _read(app_dir / "requirements.txt")
    if (app_dir / ".git").is_dir() and shutil.which("git"):
        progress("Pulling the newest version with git…")
        result = subprocess.run(["git", "-C", str(app_dir), "pull", "--ff-only"],
                                capture_output=True, text=True, creationflags=NO_WINDOW)
        if result.returncode != 0:
            raise UpdateError("git pull failed: " + (result.stderr or result.stdout).strip()[:300])
    else:
        progress("Downloading the newest version…")
        archive = _get(f"{API}/repos/{REPO}/zipball/{newest['sha']}", timeout=120)
        progress("Copying the new files…")
        _unpack(archive, app_dir)
    (app_dir / STATE_PATH.name).write_text(json.dumps(
        {"sha": newest["sha"], "date": newest.get("date", ""),
         "installed": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1))
    if _read(app_dir / "requirements.txt") != old_requirements:
        progress("Installing new Python packages (this can take a minute)…")
        result = subprocess.run([console_python(), "-m", "pip", "install", "-r",
                                 str(app_dir / "requirements.txt")],
                                capture_output=True, text=True, creationflags=NO_WINDOW)
        if result.returncode != 0:
            raise UpdateError("the new files are in place, but installing packages failed: "
                              + result.stderr.strip()[-300:])
    return f"Updated to {short(newest['sha'])} ({newest.get('message', '')})."


def _install_exe(newest, progress, exe=None):
    """Download the release's exe next to the running one and swap them. The running exe can
    be renamed but not overwritten, so it becomes '.old' (deleted at the next start)."""
    exe = Path(exe or sys.executable)
    new, old = exe.with_name(exe.name + ".new"), exe.with_name(exe.name + ".old")
    progress("Downloading the new version (about a minute)…")
    data = _get(newest["url"], timeout=600)
    if len(data) < 1_000_000 or data[:2] != b"MZ":
        raise UpdateError("the download wasn't a Windows program")
    try:
        new.write_bytes(data)
        progress("Swapping in the new version…")
        if old.exists():
            old.unlink()
        os.replace(exe, old)
        try:
            os.replace(new, exe)
        except OSError:
            os.replace(old, exe)  # put the running one back
            raise
    except OSError as e:
        new.unlink(missing_ok=True)
        raise UpdateError(f"couldn't replace the program in {exe.parent} ({e.strerror or e}); "
                          "move it to a folder you can write to, like your Desktop") from None
    return f"Updated to {newest['tag']} ({newest.get('message', '')})."


def clean_up_old_exe():
    """Delete the previous exe left behind by an update (it was still running then)."""
    if FROZEN:
        try:
            Path(sys.executable).with_name(Path(sys.executable).name + ".old").unlink(missing_ok=True)
        except OSError:
            pass


def _read(path):
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _unpack(archive, app_dir):
    """Copy a GitHub zipball's files over the app (they all sit in one top folder)."""
    with zipfile.ZipFile(io.BytesIO(archive)) as z, tempfile.TemporaryDirectory() as tmp:
        z.extractall(tmp)
        tops = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if len(tops) != 1:
            raise UpdateError("the download didn't look like the app")
        src = tops[0]
        if not (src / "hallows_eve_detector.py").exists():
            raise UpdateError("the download didn't contain the app")
        for path in src.rglob("*"):
            rel = path.relative_to(src)
            if path.is_dir() or rel.name in KEEP or rel.parts[0] == ".git":
                continue
            target = app_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def console_python():
    """python.exe (not pythonw.exe), for running pip."""
    exe = Path(sys.executable)
    plain = exe.with_name("python.exe")
    return str(plain if exe.name.lower() == "pythonw.exe" and plain.exists() else exe)


def windowless_python():
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    return str(quiet if quiet.exists() else exe)


def restart(app_dir=APP_DIR):
    """Start a fresh copy of the app (without a console window); the caller then quits."""
    detached = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    if FROZEN:
        subprocess.Popen([sys.executable], cwd=str(Path(sys.executable).parent),
                         creationflags=detached)
    else:
        subprocess.Popen([windowless_python(), str(app_dir / "hallows_eve_detector.py")],
                         cwd=str(app_dir), creationflags=detached)


if __name__ == "__main__":
    try:
        state, mine, newest = check()
        print(f"This copy: {display(mine)}   newest: {display(newest)} {newest['date']} "
              f"{newest['message']}")
        print({"up_to_date": "Up to date.", "available": "An update is available.",
               "unknown": "Can't tell which version this is."}[state])
        if state != "up_to_date" and "--install" in sys.argv[1:]:
            print(install(newest))
    except UpdateError as e:
        sys.exit(f"Update check failed: {e}")
