"""Bounded text extraction. Never executes Office macros, scripts, OCR or external links."""
from __future__ import annotations

import html
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

from .common import AssistantError


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocked: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript", "template", "form", "textarea"):
            self.blocked.append(tag)
        if not self.blocked and tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.blocked and self.blocked[-1] == tag:
            self.blocked.pop()

    def handle_data(self, data: str) -> None:
        if not self.blocked:
            self.parts.append(data)


def html_text(value: str) -> str:
    parser = VisibleText()
    parser.feed(value)
    return re.sub(r"\n[ \t]*\n+", "\n\n", "".join(parser.parts)).strip()


def vtt_text(value: str) -> tuple[str, dict]:
    blocks = re.split(r"\r?\n\s*\r?\n", value.lstrip("\ufeff"))
    result, speakers = [], set()
    for block in blocks:
        lines = block.splitlines()
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        time = lines[timing].split("-->")[0].strip()
        if not re.fullmatch(r"(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3}", time):
            continue
        text = " ".join(lines[timing + 1:]).strip()
        voice = re.search(r"<v(?:\.[^ >]+)?\s+([^>]+)>", text)
        who = html.unescape(voice.group(1)).strip() if voice else ""
        if who:
            speakers.add(who)
        clean = html.unescape(re.sub(r"<[^>]*>", "", text)).strip()
        if clean:
            result.append(f"[{time}] {who + ': ' if who else ''}{clean}")
    if not result:
        raise AssistantError("VTT_NO_CUES", "文字起こしの発言を読み取れませんでした")
    return "\n".join(result), {"cue_count": len(result), "speakers": sorted(speakers), "coverage": "text_cues"}


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    raise AssistantError("TEXT_ENCODING_UNSUPPORTED")


def _xml(data: bytes) -> ET.Element:
    declaration_scan = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declaration_scan or b"<!ENTITY" in declaration_scan:
        raise AssistantError("XML_ENTITY_BLOCKED")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise AssistantError("OFFICE_XML_INVALID") from None


def office_text(data: bytes, suffix: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(i.file_size for i in entries) > 50_000_000:
                raise AssistantError("OFFICE_ARCHIVE_LIMIT")
            if any(i.file_size > 20_000_000 or i.flag_bits & 1 for i in entries):
                raise AssistantError("OFFICE_ARCHIVE_LIMIT")
            names = set(archive.namelist())
            out: list[str] = []
            if suffix == ".docx":
                root = _xml(archive.read("word/document.xml"))
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                for p in root.findall(".//w:p", ns):
                    out.append("".join(t.text or "" for t in p.findall(".//w:t", ns)))
            elif suffix == ".pptx":
                slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.search(r"slide(\d+)", n).group(1)))
                for n in slides:
                    root = _xml(archive.read(n))
                    out.append(f"[{Path(n).stem}]\n" + "\n".join(t.text or "" for t in root.iter() if t.tag.endswith("}t")))
            else:
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                strings: list[str] = []
                if "xl/sharedStrings.xml" in names:
                    root = _xml(archive.read("xl/sharedStrings.xml"))
                    strings = ["".join(t.text or "" for t in item.findall(".//s:t", ns)) for item in root.findall("s:si", ns)]
                sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
                for name in sheets:
                    out.append(f"[{Path(name).stem}; cached values; no formula evaluation]")
                    root = _xml(archive.read(name))
                    for row in root.findall(".//s:row", ns):
                        values = []
                        for cell in row.findall("s:c", ns):
                            value = cell.findtext("s:v", default="", namespaces=ns)
                            if cell.get("t") == "s":
                                try:
                                    value = strings[int(value)]
                                except (ValueError, IndexError):
                                    raise AssistantError("XLSX_STRING_INVALID") from None
                            elif cell.get("t") == "inlineStr":
                                value = "".join(t.text or "" for t in cell.findall(".//s:t", ns))
                            values.append(f"{cell.get('r', '')}={value}")
                        out.append("\t".join(values))
            return "\n".join(out)
    except (zipfile.BadZipFile, KeyError, RuntimeError, OSError):
        raise AssistantError("OFFICE_PARSE_FAILED") from None


def extract_bytes(data: bytes, suffix: str) -> tuple[str, dict]:
    metadata = {"coverage": "text_only"}
    if suffix in (".docx", ".pptx", ".xlsx"):
        text = office_text(data, suffix)
    elif suffix == ".eml":
        msg = BytesParser(policy=policy.default).parsebytes(data)
        body = msg.get_body(preferencelist=("plain", "html"))
        if body is None:
            raise AssistantError("EMAIL_NO_BODY")
        content = body.get_content()
        text = html_text(content) if body.get_content_type() == "text/html" else content
        text = f"Subject: {msg.get('Subject', '')}\nFrom: {msg.get('From', '')}\nDate: {msg.get('Date', '')}\n\n{text}"
        metadata["coverage"] = "message_body_without_attachments"
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise AssistantError("PDF_DEPENDENCY_MISSING", "承認済pypdfが必要です。OCRは実行しません") from None
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise AssistantError("ENCRYPTED_DOCUMENT")
            if len(reader.pages) > 100:
                raise AssistantError("PDF_PAGE_LIMIT")
            text = "\n".join(f"[page {i + 1}]\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages))
            if len(re.sub(r"\[page \d+\]|\s", "", text)) < 10:
                raise AssistantError("PDF_NO_TEXT", "画像PDFのOCRは未対応です")
        except AssistantError:
            raise
        except Exception:
            raise AssistantError("PDF_PARSE_FAILED") from None
    else:
        text = decode_text(data)
        if suffix == ".vtt":
            return vtt_text(text)
        if suffix in (".html", ".htm"):
            text = html_text(text)
    if not text.strip():
        raise AssistantError("DOCUMENT_EMPTY")
    return text, metadata
