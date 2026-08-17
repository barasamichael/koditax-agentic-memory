"""Authoritative, safe document-family registry and bounded type detection.

The registry is deliberately application data rather than a collection of
route-local MIME allowlists.  Detection uses bytes and safe container metadata;
names and declared media types are corroborating signals only.
"""

# ruff: noqa: E501

from __future__ import annotations

from io import BytesIO
import json
import struct
from typing import Literal
import zipfile
from xml.etree import ElementTree
from dataclasses import dataclass

DocumentFamily = Literal["pdf", "text", "word_processing", "spreadsheet", "presentation", "image"]

MAX_IMAGE_PIXELS = 40_000_000
MAX_STRUCTURED_DEPTH = 64
MAX_CONTAINER_ENTRIES = 10_000
MAX_CONTAINER_UNCOMPRESSED_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class DocumentFormat:
    family: DocumentFamily
    extensions: tuple[str, ...]
    media_types: tuple[str, ...]
    inspection_strategy: str
    normalization_strategy: str
    preview_capable: bool


FORMATS: tuple[DocumentFormat, ...] = (
    DocumentFormat("pdf", (".pdf",), ("application/pdf",), "pdf", "page partitions", True),
    DocumentFormat("text", (".txt",), ("text/plain",), "text", "line partitions", True),
    DocumentFormat(
        "text", (".md",), ("text/markdown", "text/x-markdown"), "text", "line partitions", True
    ),
    DocumentFormat("text", (".csv",), ("text/csv",), "text", "record partitions", True),
    DocumentFormat(
        "text", (".tsv",), ("text/tab-separated-values",), "text", "record partitions", True
    ),
    DocumentFormat(
        "text", (".json",), ("application/json", "text/json"), "json", "path partitions", True
    ),
    DocumentFormat(
        "text", (".xml",), ("application/xml", "text/xml"), "xml", "path partitions", True
    ),
    DocumentFormat(
        "word_processing",
        (".docx",),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
        "ooxml",
        "structural partitions",
        True,
    ),
    DocumentFormat(
        "word_processing",
        (".odt",),
        ("application/vnd.oasis.opendocument.text",),
        "odf",
        "structural partitions",
        True,
    ),
    DocumentFormat(
        "word_processing",
        (".rtf",),
        ("application/rtf", "text/rtf"),
        "rtf",
        "paragraph partitions",
        True,
    ),
    DocumentFormat(
        "spreadsheet",
        (".xlsx",),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
        "ooxml",
        "worksheet partitions",
        True,
    ),
    DocumentFormat(
        "spreadsheet",
        (".ods",),
        ("application/vnd.oasis.opendocument.spreadsheet",),
        "odf",
        "worksheet partitions",
        True,
    ),
    DocumentFormat(
        "presentation",
        (".pptx",),
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation",),
        "ooxml",
        "slide partitions",
        True,
    ),
    DocumentFormat(
        "presentation",
        (".odp",),
        ("application/vnd.oasis.opendocument.presentation",),
        "odf",
        "slide partitions",
        True,
    ),
    DocumentFormat("image", (".png",), ("image/png",), "image", "image partitions", True),
    DocumentFormat("image", (".jpg", ".jpeg"), ("image/jpeg",), "image", "image partitions", True),
    DocumentFormat("image", (".webp",), ("image/webp",), "image", "image partitions", True),
    DocumentFormat("image", (".tif", ".tiff"), ("image/tiff",), "image", "image partitions", True),
)

MIME_ALIASES = {
    "text/x-markdown": "text/markdown",
    "text/json": "application/json",
    "text/xml": "application/xml",
    "text/rtf": "application/rtf",
}


def normalize_media_type(value: str) -> str:
    normalized = value.split(";", 1)[0].strip().lower()
    return MIME_ALIASES.get(normalized, normalized)


_MEDIA_TYPE_TO_FAMILY: dict[str, DocumentFamily] = {}
_MEDIA_TYPE_TO_EXTENSION: dict[str, str] = {}
for _format in FORMATS:
    for _media_type in _format.media_types:
        _normalized_media_type = normalize_media_type(_media_type)
        _MEDIA_TYPE_TO_FAMILY[_normalized_media_type] = _format.family
        _MEDIA_TYPE_TO_EXTENSION[_normalized_media_type] = _format.extensions[0]


def supported_media_types() -> tuple[str, ...]:
    return tuple(
        sorted(
            {media for item in FORMATS for media in item.media_types if media not in MIME_ALIASES}
        )
    )


def supported_extensions() -> tuple[str, ...]:
    return tuple(sorted({extension for item in FORMATS for extension in item.extensions}))


def is_supported_media_type(value: str) -> bool:
    return normalize_media_type(value) in supported_media_types()


def family_for_media_type(value: str) -> DocumentFamily | None:
    """Return the broad document family for one normalized media type."""

    return _MEDIA_TYPE_TO_FAMILY.get(normalize_media_type(value))


def extension_for_media_type(value: str) -> str | None:
    """Return the preferred canonical extension for one normalized media type."""

    return _MEDIA_TYPE_TO_EXTENSION.get(normalize_media_type(value))


def detect_format(payload: bytes) -> tuple[str | None, str | None, str | None]:
    """Return media type, safe rejection reason, and optional format detail."""

    if payload.startswith((b"MZ", b"\x7fELF", b"#!")):
        return None, "unsupported_format", None
    if payload.startswith(b"%PDF-"):
        return (
            "application/pdf",
            None if b"%%EOF" in payload[-2048:] else "malformed_document",
            "pdf",
        )
    if payload.startswith(b"{\\rtf"):
        return "application/rtf", None, "rtf"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png(payload)
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", None, "jpeg"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp", None, "webp"
    if payload.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff", None, "tiff"
    if payload.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return _container(payload)
    return _structured_text(payload)


def _png(payload: bytes) -> tuple[str | None, str | None, str | None]:
    if len(payload) < 24 or payload[12:16] != b"IHDR":
        return "image/png", "malformed_document", "png"
    width, height = struct.unpack(">II", payload[16:24])
    if not width or not height or width * height > MAX_IMAGE_PIXELS:
        return "image/png", "image_dimensions_too_large", "png"
    return "image/png", None, "png"


def _container(payload: bytes) -> tuple[str | None, str | None, str | None]:
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            infos = archive.infolist()
            if (
                len(infos) > MAX_CONTAINER_ENTRIES
                or sum(i.file_size for i in infos) > MAX_CONTAINER_UNCOMPRESSED_BYTES
            ):
                return None, "malformed_document", None
            names = {info.filename for info in infos}
            if any(name.startswith(("/", "\\")) or ".." in name.split("/") for name in names):
                return None, "invalid_office_container", None
            if any(name.lower().endswith("vbaProject.bin".lower()) for name in names):
                return None, "unsafe_active_content", None
            if "[Content_Types].xml" in names:
                if "word/document.xml" in names:
                    return (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        None,
                        "docx",
                    )
                if "xl/workbook.xml" in names:
                    return (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        None,
                        "xlsx",
                    )
                if "ppt/presentation.xml" in names:
                    return (
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        None,
                        "pptx",
                    )
                return None, "invalid_office_container", None
            if "mimetype" in names:
                mime = archive.read("mimetype")[:128].decode("ascii", "strict").strip()
                if (
                    mime
                    in {
                        "application/vnd.oasis.opendocument.text",
                        "application/vnd.oasis.opendocument.spreadsheet",
                        "application/vnd.oasis.opendocument.presentation",
                    }
                    and "content.xml" in names
                ):
                    return mime, None, "odf"
                return None, "invalid_office_container", None
    except zipfile.BadZipFile:
        return None, "archive_not_permitted", None
    except (OSError, UnicodeDecodeError):
        return None, "invalid_office_container", None
    return None, "archive_not_permitted", None


def _structured_text(payload: bytes) -> tuple[str | None, str | None, str | None]:
    if b"\x00" in payload[:8192]:
        return None, "unsupported_format", None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None, "unsupported_format", None
    stripped = text.lstrip()
    if not stripped:
        return "text/plain", None, None
    if stripped.startswith(("{", "[")):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return "application/json", "malformed_document", "json"
        return (
            ("application/json", "structured_text_too_deep", "json")
            if _depth(value) > MAX_STRUCTURED_DEPTH
            else ("application/json", None, "json")
        )
    if stripped.startswith("<"):
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            return "application/xml", "unsafe_active_content", "xml"
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            return "application/xml", "malformed_document", "xml"
        return (
            ("application/xml", "structured_text_too_deep", "xml")
            if _xml_depth(root) > MAX_STRUCTURED_DEPTH
            else ("application/xml", None, "xml")
        )
    if "\t" in text and "\n" in text:
        return "text/tab-separated-values", None, "tsv"
    if "," in text and "\n" in text:
        return "text/csv", None, "csv"
    return (
        "text/markdown"
        if any(line.startswith("#") for line in text.splitlines()[:20])
        else "text/plain",
        None,
        "markdown" if any(line.startswith("#") for line in text.splitlines()[:20]) else "plain",
    )


def _depth(value: object, level: int = 0) -> int:
    if not isinstance(value, (dict, list)):
        return level
    children = value.values() if isinstance(value, dict) else value
    return max([level, *(_depth(child, level + 1) for child in children)])


def _xml_depth(element: ElementTree.Element, level: int = 1) -> int:
    return max([level, *(_xml_depth(child, level + 1) for child in element)])
