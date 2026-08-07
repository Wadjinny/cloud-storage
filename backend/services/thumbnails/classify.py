from __future__ import annotations

from pathlib import PurePosixPath

Kind = str  # image | pdf | video | office | unsupported

_IMAGE_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}
_PDF_EXT = {".pdf"}
_VIDEO_EXT = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".m4v",
    ".mpeg",
    ".mpg",
}
_OFFICE_EXT = {
    ".doc",
    ".docx",
    ".odt",
    ".rtf",
    ".ppt",
    ".pptx",
    ".odp",
    ".xls",
    ".xlsx",
    ".ods",
}


def classify_filename(filename: str) -> Kind:
    ext = PurePosixPath(filename).suffix.lower()
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _PDF_EXT:
        return "pdf"
    if ext in _VIDEO_EXT:
        return "video"
    if ext in _OFFICE_EXT:
        return "office"
    return "unsupported"
