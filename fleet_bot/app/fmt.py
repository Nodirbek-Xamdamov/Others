"""Telegram HTML formatlash (bot javoblari faqat ingliz tilida).

Qiymatlarni chiroyli yozish (764,483 · Apr 1, 2027 ❗️) va LLM matnini xavfsiz HTML'ga aylantirish.
"""
from __future__ import annotations

import datetime as dt
import html
import re

SOON_DAYS = 30
PM_SOON = 3000


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=False)


def num(v) -> str:
    """764483 -> '764,483'."""
    if v is None:
        return "—"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, float):
        return f"{v:,.1f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def date(d: str | None, mark: bool = True) -> str:
    """'2027-04-01' -> 'Apr 1, 2027'; o'tgan bo'lsa ❗️, 30 kun ichida bo'lsa ⚠️."""
    if not d:
        return "—"
    try:
        day = dt.date.fromisoformat(d[:10])
    except ValueError:
        return d
    s = f"{day:%b} {day.day}, {day.year}"
    if not mark:
        return s
    today = dt.date.today()
    if day < today:
        return f"{s} ❗️"
    if day <= today + dt.timedelta(days=SOON_DAYS):
        return f"{s} ⚠️"
    return s


def today() -> str:
    d = dt.date.today()
    return f"{d:%b} {d.day}, {d.year}"


def pm(miles_until_pm: int | None) -> str:
    if miles_until_pm is None:
        return "—"
    if miles_until_pm < 0:
        return f"PM overdue by {num(-miles_until_pm)} mi ❗️"
    if miles_until_pm <= PM_SOON:
        return f"PM due in {num(miles_until_pm)} mi ⚠️"
    return f"PM due in {num(miles_until_pm)} mi"


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")


def cell(col: str, v) -> str:
    """SQL natijasidagi bitta qiymatni ustun nomiga qarab formatlaydi."""
    if v is None:
        return "—"
    c = col.lower()
    if isinstance(v, str) and _ISO_DATE.match(v):
        return date(v) + (v[10:] if len(v) > 10 else "")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "year" in c:
            return str(int(v)) if float(v).is_integer() else str(v)
        if "until_pm" in c:
            return f"{num(v)} ❗️" if v < 0 else num(v)
        if "pct" in c or "percent" in c:
            return f"{v:.0f}%"
        return num(v)
    return str(v)


# --------------------------------------------------------------------------- #
# LLM javobi (markdown-lite) -> Telegram HTML
# --------------------------------------------------------------------------- #

_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_EMPTY_CELLS = {"", "-", "–", "—"}


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def tables_to_text(text: str) -> str:
    """Markdown jadvalni Telegram'da o'qiladigan qatorlarga aylantiradi (model qoidaga amal qilmasa ham)."""
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        if i + 1 < len(lines) and "|" in lines[i] and _TABLE_SEP.match(lines[i + 1]):
            header, rows = _cells(lines[i]), []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            if len(rows) <= 3 and len(header) > 4:  # kam qator, ko'p ustun -> har qator alohida karta
                for r in rows:
                    out += [f"• {h}: {v}" for h, v in zip(header, r) if v not in _EMPTY_CELLS] + [""]
            else:
                out += ["• " + " · ".join(v for v in r if v not in _EMPTY_CELLS) for r in rows]
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def md_to_html(text: str) -> str:
    """LLM matni: **qalin**, `kod`, # sarlavha, - ro'yxat -> Telegram HTML. Qolgan hammasi escape qilinadi."""
    text = tables_to_text(text)
    text = esc(text)
    text = re.sub(r"(?m)^\s*#{1,6}\s*(.+?)\s*#*\s*$", r"<b>\1</b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])__(.+?)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = text.replace("`", "")
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_html(text: str) -> str:
    """HTML'ni Telegram rad etsa — oddiy matn sifatida yuborish uchun."""
    return html.unescape(re.sub(r"</?(b|i|u|s|code|pre|a)(\s[^>]*)?>", "", text))
