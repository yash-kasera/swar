"""Document -> plain text conversion. Deliberately rule-based: no AI is used here."""
from __future__ import annotations

import html
import io
import re
from html.parser import HTMLParser

SUPPORTED = {".txt", ".md", ".markdown", ".docx", ".pdf", ".html", ".htm", ".rtf"}


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _strip_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)           # code fences
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)            # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)        # links -> label
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)   # headings
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.M)        # blockquotes
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)        # bullets
    text = re.sub(r"^\s*([-*_]\s*){3,}$", "", text, flags=re.M) # rules
    text = re.sub(r"`([^`]*)`", r"\1", text)                    # inline code
    # Bold/underline markers. Single *asterisks* are kept: stories often use
    # them for stage directions (*Yash runs away*), which the LLM should see.
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    return text


def _rtf_to_text(text: str) -> str:
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252"), text)
    text = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    return text.replace("{", "").replace("}", "")


def _normalise(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def convert(filename: str, raw: bytes) -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED:
        raise ValueError(f"Unsupported file type '{ext or filename}'. Use: {', '.join(sorted(SUPPORTED))}")

    if ext == ".docx":
        import docx  # python-docx

        d = docx.Document(io.BytesIO(raw))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        text = "\n".join(parts)
    elif ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    elif ext in (".html", ".htm"):
        p = _HTMLText()
        p.feed(_decode(raw))
        text = html.unescape("".join(p.parts))
    elif ext == ".rtf":
        text = _rtf_to_text(_decode(raw))
    elif ext in (".md", ".markdown"):
        text = _strip_markdown(_decode(raw))
    else:
        text = _decode(raw)

    text = _normalise(text)
    if not text:
        raise ValueError("No readable text found in the document (scanned PDFs need OCR, which this tool does not do).")
    return text
