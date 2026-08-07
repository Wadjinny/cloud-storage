from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

from config import THUMB_GEN_TIMEOUT, THUMB_MAX_EDGE, THUMB_MAX_SOURCE_BYTES
from services.thumbnails.classify import Kind, classify_filename

log = logging.getLogger(__name__)


class ThumbGenError(RuntimeError):
    pass


def _fit_image(im: object) -> bytes:
    from PIL import Image

    assert isinstance(im, Image.Image)
    im = im.convert("RGBA") if im.mode in ("P", "RGBA") else im.convert("RGB")
    im.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.Resampling.LANCZOS)
    if im.mode != "RGB":
        background = Image.new("RGB", im.size, (32, 32, 32))
        background.paste(im, mask=im.split()[-1] if im.mode == "RGBA" else None)
        im = background
    buf = BytesIO()
    im.save(buf, format="WEBP", quality=80, method=4)
    return buf.getvalue()


def _from_image(path: Path) -> bytes:
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        return _fit_image(im)


def _from_pdf(path: Path) -> bytes:
    import fitz
    from PIL import Image

    doc = fitz.open(path)
    try:
        if doc.page_count < 1:
            raise ThumbGenError("PDF has no pages")
        page = doc.load_page(0)
        # Scale so the longer edge ~ THUMB_MAX_EDGE at 72 dpi base.
        rect = page.rect
        scale = THUMB_MAX_EDGE / max(rect.width, rect.height, 1)
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return _fit_image(im)
    finally:
        doc.close()


def _which_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _from_office(path: Path) -> bytes:
    soffice = _which_soffice()
    if soffice is None:
        raise ThumbGenError("LibreOffice (soffice) not installed")
    with tempfile.TemporaryDirectory(prefix="thumb-office-") as tmp:
        out_dir = Path(tmp)
        try:
            subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--nologo",
                    "--nolockcheck",
                    "--nodefault",
                    "--norestore",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(out_dir),
                    str(path),
                ],
                check=True,
                capture_output=True,
                timeout=THUMB_GEN_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise ThumbGenError("LibreOffice timed out") from exc
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or b"").decode("utf-8", errors="replace")[:300]
            raise ThumbGenError(f"LibreOffice failed: {err}") from exc
        pdfs = list(out_dir.glob("*.pdf"))
        if not pdfs:
            raise ThumbGenError("LibreOffice produced no PDF")
        return _from_pdf(pdfs[0])


def _from_video(path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ThumbGenError("ffmpeg not installed")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        out = Path(tmp.name)
    try:
        # Prefer 2s; fall back for shorter clips (1s, then first frame).
        last_err = ""
        for seek in ("2", "1", None):
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
            if seek is not None:
                cmd.extend(["-ss", seek])
            cmd.extend(
                [
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    "-y",
                    str(out),
                ]
            )
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    capture_output=True,
                    timeout=THUMB_GEN_TIMEOUT,
                )
            except subprocess.TimeoutExpired as exc:
                raise ThumbGenError("ffmpeg timed out") from exc
            except subprocess.CalledProcessError as exc:
                last_err = (exc.stderr or b"").decode("utf-8", errors="replace")[:300]
                continue
            if out.exists() and out.stat().st_size > 0:
                return _from_image(out)
        if last_err:
            raise ThumbGenError(f"ffmpeg failed: {last_err}")
        raise ThumbGenError("ffmpeg produced empty frame")
    finally:
        out.unlink(missing_ok=True)


def generate_webp(path: Path, filename: str) -> tuple[bytes, Kind]:
    """Return (webp_bytes, kind). Raises ThumbGenError on failure."""
    kind = classify_filename(filename)
    if kind == "unsupported":
        raise ThumbGenError("unsupported type")
    size = path.stat().st_size
    # Office conversion needs the whole file; video thumbs use a small prefix spool.
    if kind == "office" and size > THUMB_MAX_SOURCE_BYTES:
        raise ThumbGenError(
            f"source too large for office thumb ({size} > {THUMB_MAX_SOURCE_BYTES})"
        )
    if kind == "image":
        return _from_image(path), kind
    if kind == "pdf":
        return _from_pdf(path), kind
    if kind == "video":
        return _from_video(path), kind
    if kind == "office":
        return _from_office(path), kind
    raise ThumbGenError(f"unknown kind {kind}")
