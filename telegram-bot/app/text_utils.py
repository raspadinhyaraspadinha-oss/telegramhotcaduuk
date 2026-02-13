import html as _html
import re


_IMG_ALT_RE = re.compile(r'<img\s+[^>]*alt="([^"]+)"[^>]*>', flags=re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_telegram_export_text(text: str) -> str:
    """
    You pasted text copied from Telegram Web export HTML.
    Telegram Bot API does NOT support most HTML tags used there.
    This function:
    - replaces <img ... alt="X"> with X (emoji)
    - strips remaining tags
    - unescapes HTML entities
    - normalizes CRLF
    """
    if not text:
        return ""

    t = text.replace("\r\n", "\n")
    t = _IMG_ALT_RE.sub(lambda m: m.group(1), t)
    t = _TAG_RE.sub("", t)
    t = _html.unescape(t)
    return t.strip()


def collapse_whitespace_one_line(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    # keep room for ellipsis
    return text[: max(0, limit - 1)].rstrip() + "…"

