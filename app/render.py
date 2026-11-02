"""Slide pictures for the review screen. Where PowerPoint is installed (Windows), it makes them through its automation
interface, driven from PowerShell: the deck opens read-only and hidden, and PowerPoint is left running when the
designer has other decks open. Elsewhere (the connector's Docker image on Render), LibreOffice does: it turns a copy
of the deck holding only the wanted slides into a PDF, and pypdfium2 draws its pages. LibreOffice substitutes fonts
it doesn't have, so its pictures can differ a little from PowerPoint's."""
import base64
import os
import shutil
import subprocess
import sys
import tempfile
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
    """Slide pictures can be made here: by PowerPoint or LibreOffice."""
    return _powerpoint() or _soffice() is not None


def _powerpoint() -> bool:
    """PowerPoint is installed here (its automation interface is registered)."""
    if sys.platform != "win32":
        return False
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "PowerPoint.Application"))
        return True
    except OSError:
        return False


def _soffice() -> str | None:
    """LibreOffice's command, if it is installed."""
    found = shutil.which("soffice") or shutil.which("libreoffice")
    windows = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "LibreOffice/program/soffice.exe"
    return found or (str(windows) if windows.exists() else None)


def slides(deck: Path, numbers: list[int], out_dir: Path, aspect: float, width: int = WIDTH_PX) -> None:
    """Pictures of the given slides (1-based) as out_dir/slide-<n>.png, `width` px wide at the slide's
    proportions (`aspect`: width / height). Raises RuntimeError when they can't be made."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if not _powerpoint():
        return _libreoffice(deck, sorted(set(numbers)), out_dir, width)
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


def only(deck: Path, numbers: list[int], dest: Path) -> None:
    """A copy of the deck holding just these slides (1-based), in their order and all shown: LibreOffice leaves
    hidden slides out of a PDF, which would shift every page after them."""
    from pptx import Presentation
    prs = Presentation(deck)
    ids = prs.slides._sldIdLst
    for k, sld_id in reversed(list(enumerate(list(ids), 1))):
        if k not in numbers:
            ids.remove(sld_id)
            prs.part.drop_rel(sld_id.rId)
    for slide in prs.slides:
        slide._element.attrib.pop("show", None)
    prs.save(dest)


def _libreoffice(deck: Path, numbers: list[int], out_dir: Path, width: int) -> None:
    soffice = _soffice()
    if soffice is None:
        raise RuntimeError("neither PowerPoint nor LibreOffice is installed where the tool runs")
    import pypdfium2
    with _lock, tempfile.TemporaryDirectory(prefix="slides-") as tmp:
        work = Path(tmp)
        only(deck, numbers, work / "slides.pptx")
        command = [soffice, f"-env:UserInstallation={(work / 'profile').as_uri()}",   # its own profile: no lock clash
                   "--headless", "--norestore", "--convert-to", "pdf", "--outdir", str(work), str(work / "slides.pptx")]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"LibreOffice couldn't export the slides ({e})")
        pdf_path = work / "slides.pdf"
        if done.returncode or not pdf_path.exists():
            lines = [line for line in (done.stderr or done.stdout or "").splitlines() if line.strip()]
            raise RuntimeError(f"LibreOffice couldn't export the slides ({lines[-1].strip() if lines else 'no reason given'})")
        pdf = pypdfium2.PdfDocument(str(pdf_path))
        try:
            if len(pdf) != len(numbers):
                raise RuntimeError(f"LibreOffice exported {len(pdf)} pages for {len(numbers)} slides")
            for n, page in zip(numbers, pdf):
                page.render(scale=width / page.get_width()).to_pil().save(out_dir / f"slide-{n}.png")
        finally:
            pdf.close()
