"""Guruhdagi invoys xabarini (fayl caption'i) maydonlarga ajratadi. Tarmoq yo'q — sof funksiyalar.

Kutilgan ko'rinish (kalitlar katta-kichik harfga befarq, tartib erkin, oxirida ustaxona bloki):

    Unit# 3968
    Driver: John Smith
    Date: 10/05/2026
    Amount: $ 70
    For: Truck tire repair
    Note: Paid with EFS
    Money Transfer code 3885738103
    Ref# 216117
    Responsible: Owner of the truck

    Tread Source Commercial Tires & Service
    1305 S Shiloh Rd, Garland, TX 75042, United States
    +14692687464

Real xabarlardagi farqlar ham qabul qilinadi (Fleetio'dagi 600 ta eski yozuv bo'yicha): "Unit: 594118 RYL",
"Truck #: 8658", "Trailer#: 44319", "Ref: 091 343413", "Report Reference # 4011078343,", "Amount: 714.14 $",
"$40,00", "$ 1,234.56", "Date: 10/5/2026", "Drivers: A & B", kalitsiz davom qatorlari ("Money Transfer code ...").
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_STATE_BY_NAME = {v.lower(): k for k, v in US_STATES.items()}

_NA = {"na", "n/a", "n\\a", "n.a", "n.a.", "none", "no", "-", "--", "tbd", "unknown", "?"}

# kalit -> qator boshidagi andoza (1-guruh = qiymat). Tartib muhim: "trailer" "unit"dan oldin tekshirilmaydi, lekin
# "report reference" "ref"ning ichida.
_KEY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("unit", re.compile(r"(?i)^unit\s*(?:#\s*:?|:|no\.?\s*:?)\s*(.*)$")),
    ("unit", re.compile(r"(?i)^truck\s*(?:#\s*:?|:|no\.?\s*:)\s*(.*)$")),
    ("unit", re.compile(r"(?i)^unit\s+(\d.*)$")),
    ("trailer", re.compile(r"(?i)^trailer\s*(?:#\s*:?|:|no\.?\s*:)\s*((?:[A-Za-z]{0,4}\d|n/?a\b).*)$")),
    ("trailer", re.compile(r"(?i)^trailer\s+(\d[\w-]*)[\s,.]*$")),
    ("driver", re.compile(r"(?i)^drivers?(?:\s+name)?\s*[:#]\s*(.*)$")),
    ("date", re.compile(r"(?i)^date\s*[:#-]\s*(.*)$")),
    ("date", re.compile(r"(?i)^date\s+(\d.*)$")),
    ("amount", re.compile(r"(?i)^amount\s*[:#-]?\s*(.*)$")),
    ("total", re.compile(r"(?i)^total(?:\s+amount)?\s*[:#-]\s*(.*)$")),
    ("for", re.compile(r"(?i)^for\s*[:#-]\s*(.*)$")),
    ("note", re.compile(r"(?i)^notes?\s*[:#-]\s*(.*)$")),
    ("ref", re.compile(r"(?i)^(?:report\s+)?ref(?:erence)?\b\.?\s*(?:#|number\b|(?:no|num)\b\.?)?\s*[:#]?\s*(.*)$")),
    ("ref", re.compile(r"(?i)^invoice\s*(?:#|number\b|(?:no|num)\b\.?)?\s*[:#]\s*(.*)$")),
    ("responsible", re.compile(r"(?i)^respon\w*(?:\s+party)?\s*[:#-]\s*(.*)$")),
    ("vendor", re.compile(r"(?i)^(?:vendor|shop(?:\s+name)?|store)\s*[:#-]\s*(.*)$")),
]

_PHONE_LINE = re.compile(r"^(?:(?:tel|phone|ph)\b[.:]?\s*)?(\+?[\d\s().\-]{7,})$", re.I)
_URL_LINE = re.compile(r"(?i)^(?:https?://|www\.)\S+$")
_COUNTRY_US = re.compile(r"(?i)[,\s]+(?:united states of america|united states|u\.s\.a\.?|usa|us)\.?$")
_COUNTRY_OTHER = re.compile(r"(?i),\s*(canada|mexico|méxico)\.?$")
_ZIP_STATE = re.compile(r"\b([A-Za-z]{2})\.?,?\s+(\d{5})(?:-\d{4})?\b")
_ADDR = re.compile(r"^(?:(?P<street>.*?),\s*)?(?P<city>[^,\d][^,]*?),\s*(?P<state>[A-Za-z]{2}|[A-Za-z][A-Za-z ]{3,})\.?,?\s+"
                   r"(?P<zip>\d{5})(?:-\d{4})?$")
# Responsible/Note'dan keyin kalitsiz kelgan, lekin ustaxona EMAS qatorlar ("CAC", "Extended for 5 days", ...)
_NOT_VENDOR = re.compile(r"(?i)^(?:money\b|transfer\b|code\b|paid\b|payment\b|extended\b|charge\b|cacs?\b|efs\b|check\b|"
                         r"card\b|cash\b|zelle\b|company\b|owner\b|driver\b|@)")
# "For"/"Note" matnida shulardan biri bo'lsa — Emergency, bo'lmasa Scheduled (RS = road service, RC = road call)
_EMERGENCY = re.compile(r"(?i)\b(?:road\s*-?\s*(?:side|service|call)|rs|rc)\b")


@dataclass
class Vendor:
    name: str
    raw_address: str = ""
    street: str = ""
    city: str = ""
    region: str = ""        # to'liq nom: "Texas" (Fleetio saytida qo'lda yaratilganlar shunday)
    region_code: str = ""   # "TX"
    postal_code: str = ""
    country: str = ""       # "US"
    phone: str = ""         # 10 ta raqam

    @property
    def has_location(self) -> bool:
        return bool(self.postal_code or self.street or self.phone)

    @property
    def phone_pretty(self) -> str:
        p = self.phone
        return f"({p[:3]}) {p[3:6]}-{p[6:]}" if len(p) == 10 else p


@dataclass
class Invoice:
    unit: str = ""
    trailer: str = ""
    drivers: list[str] = field(default_factory=list)
    date: dt.date | None = None
    date_raw: str = ""
    amount: Decimal | None = None
    amount_raw: str = ""
    purpose: str = ""          # "For:"
    note: str = ""
    ref: str = ""
    responsible: str = ""
    vendor: Vendor | None = None
    description: str = ""      # ustaxona blokisiz matn — work order "Description"iga ketadi
    emergency: bool = False
    problems: list[str] = field(default_factory=list)   # to'ldirilmasa work order yaratilmaydi
    warnings: list[str] = field(default_factory=list)   # ixtiyoriy maydon yo'q/noto'g'ri — bo'sh qoldiriladi

    @property
    def ok(self) -> bool:
        return not self.problems


def _clean(line: str) -> str:
    line = line.replace("\xa0", " ").replace("​", "").replace("﻿", "").strip()
    return re.sub(r"^[^\w+(#@$]+", "", line).strip()  # boshidagi bullet/emoji


def _match_key(line: str) -> tuple[str, str] | None:
    for key, pat in _KEY_PATTERNS:
        m = pat.match(line)
        if m:
            return key, m.group(1).strip()
    return None


def _is_na(s: str) -> bool:
    return s.strip().lower().strip(".") in _NA


def _strip_value(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().strip(",;").strip()


def parse_amount(s: str) -> Decimal | None:
    """'$ 70' -> 70.00, '$ 1,234.56' -> 1234.56, '$40,00' -> 40.00, '714.14 $' -> 714.14. Noaniq bo'lsa None."""
    t = re.sub(r"(?i)\busd\b|dollars?|\$", " ", s.replace("\xa0", " "))
    nums = re.findall(r"\d[\d.,]*", t)
    if len(nums) != 1:
        return None
    n = nums[0].rstrip(".,")
    if "," in n and "." in n:
        n = n.replace(".", "").replace(",", ".") if n.rfind(",") > n.rfind(".") else n.replace(",", "")
    elif "," in n:
        n = n.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", n) else n.replace(",", ".")
    if n.count(".") > 1:
        return None
    try:
        v = Decimal(n).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return v if v > 0 else None


def parse_date(s: str) -> dt.date | None:
    """AQSh tartibi: MM/DD/YYYY (ajratuvchi / - .). YYYY-MM-DD ham. Oy > 12 bo'lsa kun/oy almashgan deb olinadi."""
    s = s.strip().strip(".,;")
    m = re.fullmatch(r"(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})", s)
        if not m:
            return None
        mo, d, y = (int(x) for x in m.groups())
        if y < 100:
            y += 2000
        if mo > 12 >= d:
            mo, d = d, mo
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def parse_address(addr: str, v: Vendor) -> None:
    """'1305 S Shiloh Rd, Garland, TX 75042, United States' -> street/city/region/zip/country."""
    a = re.sub(r"\s+", " ", addr).strip().strip(",")
    v.raw_address = a
    if not a:
        return
    m = _COUNTRY_OTHER.search(a)
    if m:  # AQShdan tashqari: bo'laklamaymiz
        v.street, v.country = a[:m.start()].strip(), m.group(1).title()
        return
    us = _COUNTRY_US.search(a)
    if us:
        a = a[:us.start()].strip().strip(",")
    m = _ADDR.match(a)
    if not m:
        v.street = a
        z = _ZIP_STATE.search(a)
        if z and z.group(1).upper() in US_STATES:
            v.region_code, v.postal_code = z.group(1).upper(), z.group(2)
            v.region, v.country = US_STATES[v.region_code], "US"
        elif us:
            v.country = "US"
        return
    state = m.group("state").strip()
    code = state.upper() if state.upper() in US_STATES else _STATE_BY_NAME.get(state.lower(), "")
    v.street = (m.group("street") or "").strip()
    v.city = m.group("city").strip()
    v.postal_code = m.group("zip")
    v.region_code = code
    v.region = US_STATES.get(code, state)
    v.country = "US" if (code or us) else ""


def parse_vendor(lines: list[str]) -> Vendor | None:
    """Ustaxona bloki: 1-qator nom, keyingilari manzil, alohida qatorda telefon."""
    phone, rest = "", []
    for line in lines:
        line = line.strip()
        if not line or _URL_LINE.match(line):
            continue
        m = _PHONE_LINE.match(line)
        digits = re.sub(r"\D", "", m.group(1)) if m else ""
        if m and 10 <= len(digits) <= 11 and (len(digits) == 10 or digits[0] == "1"):
            phone = phone or digits[-10:]
            continue
        rest.append(line)
    if not rest:
        return None
    name, addr_lines = rest[0], rest[1:]
    if not addr_lines:  # bitta qatorda: "Name, 123 Main St, City, ST 12345"
        m = re.match(r"^(.*?[A-Za-z].*?),\s*(\d+\s+\S.*\b\d{5}\b.*)$", name)
        if m:
            name, addr_lines = m.group(1), [m.group(2)]
    v = Vendor(name=_strip_value(name).strip("."), phone=phone)
    parse_address(", ".join(addr_lines), v)
    return v


def _has_location(lines: list[str]) -> bool:
    """Qatorlar orasida manzil (shtat + zip / 'United States') yoki telefon bormi."""
    for line in lines:
        digits = re.sub(r"\D", "", line)
        if (_PHONE_LINE.match(line) and len(digits) >= 10) or _ZIP_STATE.search(line) or _COUNTRY_US.search(line):
            return True
    return False


def _looks_like_vendor(lines: list[str]) -> bool:
    lines = [x for x in lines if x.strip()]
    if not lines or _NOT_VENDOR.match(lines[0]):
        return False
    if _has_location(lines):
        return True
    # faqat nom: kamida 2 so'z yoki 6+ harf ("CAC" kabi qisqa davom qatorlari ustaxona emas)
    first = lines[0]
    return len(re.findall(r"[A-Za-z]", first)) >= 6 or len(first.split()) >= 2


def looks_like_invoice(text: str | None) -> bool:
    """Matn invoys formatidami: unit/trailer, date, amount, for kalitlaridan kamida 3 tasi yoki majburiy ikkitasi
    (unit va date) bor."""
    if not text:
        return False
    found = set()
    for raw in text.splitlines():
        hit = _match_key(_clean(raw))
        if hit:
            found.add({"trailer": "unit", "total": "amount"}.get(hit[0], hit[0]))
    return len(found & {"unit", "date", "amount", "for"}) >= 3 or {"unit", "date"} <= found


def split_names(s: str) -> list[str]:
    """'Joshua Isaac &  Eliu Fatu' -> ['Joshua Isaac', 'Eliu Fatu']; NA tashlanadi."""
    out = []
    for part in re.split(r"\s*(?:&|\+|/|,|;|\band\b)\s*", s, flags=re.I):
        part = _strip_value(part).strip(".")
        if part and not _is_na(part):
            out.append(part)
    return out


def parse_invoice(text: str, today: dt.date | None = None) -> Invoice:
    """Caption matni -> Invoice. Yetishmagan/noto'g'ri majburiy maydonlar inv.problems'da (inglizcha, foydalanuvchiga)."""
    today = today or dt.date.today()
    lines = [_clean(x) for x in (text or "").splitlines()]
    kinds: list[tuple[str, str, str]] = []   # ("key", kalit, qiymat) | ("free", "", matn) | ("blank", "", "")
    for line in lines:
        if not line:
            kinds.append(("blank", "", ""))
            continue
        hit = _match_key(line)
        kinds.append(("key", hit[0], hit[1]) if hit else ("free", "", line))

    key_idx = [i for i, k in enumerate(kinds) if k[0] == "key"]
    inv = Invoice()
    if not key_idx:
        inv.problems.append("The message is not in the invoice format (Unit / Date / Amount / For lines).")
        return inv
    first, last = key_idx[0], key_idx[-1]

    values: dict[str, str] = {}
    cont: dict[str, list[str]] = {}
    cur = ""
    for i in range(first, last + 1):
        kind, key, val = kinds[i]
        if kind == "key":
            cur = key
            if key not in values or not values[key]:
                values[key] = val
            elif key == "ref":
                pass  # birinchi Ref ustun
        elif kind == "free" and cur:
            cont.setdefault(cur, []).append(val)

    # oxirgi kalitdan keyingi qatorlar: bo'sh qatorgacha — davom, bo'sh qatordan keyin — ustaxona bloki
    after_direct, after_blank, seen_blank = [], [], False
    for kind, _, val in kinds[last + 1:]:
        if kind == "blank":
            seen_blank = True
            continue
        (after_blank if seen_blank else after_direct).append(val)
    head = [val for kind, _, val in kinds[:first] if kind == "free"]

    vendor_lines: list[str] = []
    used_blank = False
    if values.get("vendor"):  # ochiq "Vendor: ..." qatori
        vendor_lines = [values["vendor"]] + cont.pop("vendor", [])
        if kinds[last][1] == "vendor":  # oxirgi kalit bo'lsa manzil/telefon keyingi qatorlarda
            vendor_lines += after_direct + after_blank
            after_direct, used_blank = [], True
    elif _looks_like_vendor(after_blank):
        vendor_lines, used_blank = after_blank, True
    elif _has_location(after_direct) and not _NOT_VENDOR.match(after_direct[0]):  # bo'sh qatorsiz yozilgan blok
        vendor_lines, after_direct = after_direct, []
    elif _looks_like_vendor(head):
        vendor_lines = head
    if not used_blank:
        after_direct = after_direct + after_blank  # ustaxona emas ekan -> oxirgi kalitning davomi
    if after_direct:
        cont.setdefault(kinds[last][1], []).extend(after_direct)
    inv.vendor = parse_vendor(vendor_lines) if vendor_lines else None

    # ---- maydonlar
    unit = _strip_value(values.get("unit", ""))
    inv.unit = "" if _is_na(unit) else unit.strip(".")
    trailer = _strip_value(values.get("trailer", ""))
    inv.trailer = "" if _is_na(trailer) else trailer.strip(".")
    inv.drivers = split_names(values.get("driver", ""))
    inv.purpose = _strip_value(" ".join([values.get("for", "")] + cont.get("for", []))).rstrip(". ")
    inv.note = "\n".join(x for x in [values.get("note", "")] + cont.get("note", []) if x).strip()
    inv.responsible = _strip_value(" ".join([values.get("responsible", "")] + cont.get("responsible", [])))
    ref = _strip_value(values.get("ref", "")).lstrip("#: ").strip()
    inv.ref = "" if _is_na(ref) else ref

    inv.date_raw = values.get("date", "").strip()
    inv.date = parse_date(inv.date_raw) if inv.date_raw else None
    inv.amount_raw = (values.get("amount") or values.get("total") or "").strip()
    inv.amount = parse_amount(inv.amount_raw) if inv.amount_raw else None

    # ---- description: ustaxona blokisiz asl matn
    vset = set(vendor_lines)
    inv.description = "\n".join(x for x in lines if x and x not in vset)
    inv.emergency = bool(_EMERGENCY.search(" ".join([inv.purpose, inv.note])))

    # ---- majburiy maydonlar (Fleetio'da ham majburiy: Vehicle va Issue Date); qolganlari bo'lmasa bo'sh qoladi
    if not inv.unit and not inv.trailer:
        inv.problems.append("Unit number is missing (line <code>Unit# 1234</code>).")
    if not inv.date_raw:
        inv.problems.append("Date is missing (line <code>Date: MM/DD/YYYY</code>).")
    elif inv.date is None:
        inv.problems.append(f"Date <code>{_esc(inv.date_raw)}</code> is not a valid date (expected MM/DD/YYYY).")
    elif inv.date > today + dt.timedelta(days=1):
        inv.problems.append(f"Date <code>{_esc(inv.date_raw)}</code> is in the future.")
    elif inv.date < today - dt.timedelta(days=730):
        inv.problems.append(f"Date <code>{_esc(inv.date_raw)}</code> is more than 2 years ago — check the year.")
    if not inv.amount_raw:
        inv.warnings.append("No Amount in the message — the line item has no cost.")
    elif inv.amount is None:
        inv.warnings.append(f"Amount <code>{_esc(inv.amount_raw)}</code> is not a single valid number — the line item has no cost.")
    if not inv.purpose:
        inv.warnings.append("No <code>For:</code> line — no issue and no service task; the file is attached to the work order.")
    return inv


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
