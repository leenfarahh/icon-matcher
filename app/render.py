"""Slide pictures for the review screen, made by PowerPoint through its automation interface (Windows; driven from
PowerShell, so no extra Python package is needed). The deck opens read-only and hidden, and PowerPoint is left
running when the designer has other decks open. TODO: LibreOffice (soffice to PDF, then PyMuPDF) for a server."""
import base64
import os
import subprocess
import sys
import threading
from pathlib import Path

WIDTH_PX = 1600
TIMEOUT_S = 300
_lock = threading.Lock()   # one export at a time: every session shares the one PowerPoint process

SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$app = New-Object -ComObject PowerPoint.Application
$pres = $app.Presentations.Open($env:DECK, -1, 0, 0)   # read-only, not untitled, no window
try {
  foreach ($n in ($env:SLIDES -split ',')) {
    $pres.Slides.Item([int]$n).Export((Join-Path $env:OUT "slide-$n.png"), 'PNG', [int]$env:W, [int]$env:H)
  }
} finally {
  $pres.Close()
  if ($app.Presentations.Count -eq 0) { $app.Quit() }
  [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
"""


def available() -> bool:
    """PowerPoint is installed here (its automation interface is registered)."""
    if sys.platform != "win32":
        return False
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "PowerPoint.Application"))
        return True
    except OSError:
        return False


def slides(deck: Path, numbers: list[int], out_dir: Path, aspect: float, width: int = WIDTH_PX) -> None:
    """Pictures of the given slides (1-based) as out_dir/slide-<n>.png, `width` px wide at the slide's
    proportions (`aspect`: width / height). Raises RuntimeError when PowerPoint can't make them."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DECK": str(Path(deck).resolve()), "OUT": str(out_dir.resolve()),
           "SLIDES": ",".join(str(n) for n in numbers), "W": str(width), "H": str(round(width / aspect))}
    script = base64.b64encode(SCRIPT.encode("utf-16-le")).decode()
    with _lock:
        try:
            done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", script],
                                  env=env, capture_output=True, text=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"PowerPoint couldn't export the slides ({e})")
    if done.returncode:
        lines = [line for line in done.stderr.splitlines() if line.strip()]
        raise RuntimeError(f"PowerPoint couldn't export the slides ({lines[0].strip() if lines else 'no reason given'})")
