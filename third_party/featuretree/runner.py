"""runner.py — run a script under FreeCAD's headless freecadcmd (its own Python 3.11).

FreeCAD is driven through its OWN interpreter (build123d/OCP can't author a native
feature tree). We locate (or extract once, cached) the AppImage's freecadcmd and run
the emitter/reader under it. Data is passed via ENV vars, never argv (freecadcmd treats
extra path args as documents to open). Override with FREECAD_CMD or FREECAD_APPIMAGE.
"""
import os
import subprocess
from pathlib import Path

DEFAULT_APPIMAGE = "/opt/FreeCAD_1.0.2-conda-Linux-x86_64-py311.AppImage"
CACHE = Path.home() / ".cache" / "featuretree" / "freecad"

# Known places a freecadcmd may already be extracted (avoid re-extracting).
_CANDIDATES = [
    "/tmp/squashfs-root/usr/bin/freecadcmd",
    str(Path.home() / ".cache" / "featuretree" / "freecad" / "squashfs-root/usr/bin/freecadcmd"),
]


def freecadcmd_path() -> str:
    env = os.environ.get("FREECAD_CMD")
    if env and Path(env).exists():
        return env
    for p in _CANDIDATES:
        if Path(p).exists():
            return p
    appimage = os.environ.get("FREECAD_APPIMAGE", DEFAULT_APPIMAGE)
    if not Path(appimage).exists():
        raise FileNotFoundError(
            f"FreeCAD AppImage not found: {appimage}. Set FREECAD_APPIMAGE or FREECAD_CMD.")
    CACHE.mkdir(parents=True, exist_ok=True)
    subprocess.run([appimage, "--appimage-extract"], cwd=str(CACHE), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(CACHE / "squashfs-root/usr/bin/freecadcmd")


def run_in_freecad(script, env_vars=None):
    cmd = [freecadcmd_path(), str(script)]
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    if env_vars:
        env.update({k: str(v) for k, v in env_vars.items()})
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


if __name__ == "__main__":
    print("freecadcmd:", freecadcmd_path())
