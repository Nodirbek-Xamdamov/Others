"""Tez-tez beriladigan savollarni LLM'siz aniqlash (o'zbek lotin/kirill, rus, ingliz).

Qoida: savoldagi har bir so'z shu intent lug'atida bo'lishi kerak. Notanish so'z bo'lsa
(masalan guruh nomi, marka, yil) — savol LLM'ga o'tadi. Shunda router xato javob bermaydi,
eng yomoni sekinroq (LLM) yo'lga ketadi.
Lug'atda 3 harfgacha bo'lgan so'zlar aniq, uzunroqlari o'zak (prefiks) bo'yicha solishtiriladi.
"""
from __future__ import annotations

import re

from . import quick
from .agent import SqlRunner

# "417", "0832 ED", "unit 3871", "#417", "3AKJHHDR1MSMS3871" — faqat raqam/VIN/plate yozilgan xabar
UNIT_ONLY = re.compile(r"(?i)^\s*(?:unit|truck|trak|трак|юнит|#|№)?\s*([a-z0-9]{1,17}(?:[ -][a-z]{1,4})?)\s*[.?!]*\s*$")

_APOS = str.maketrans({c: "'" for c in "‘’ʻʼ`´"})
_TOKEN = re.compile(r"\d+|[^\W\d_]+(?:'[^\W\d_]+)*")

_GENERIC = (
    "mashina", "moshina", "truck", "trak", "trek", "unit", "avto", "машин", "мошин", "трак", "грузовик", "юнит", "тягач",
    "qaysi", "qays", "which", "какие", "какой", "каких", "ro'yxat", "royxat", "list", "список", "ko'rsat", "korsat",
    "show", "покаж", "bor", "bormi", "есть", "there", "are", "is", "the", "a", "of", "our", "we", "have", "do", "in",
    "all", "bizda", "bizning", "hozir", "now", "сейчас", "hamma", "barcha", "все", "всех", "jami", "please", "iltimos",
    "пожалуйста", "kerak", "need", "нужн", "menga", "me", "мне", "ber", "bering", "give", "дай", "qil", "qiling", "ayt",
    "ayting", "скажи", "tell", "va", "and", "и", "lar", "ni", "da", "dagi", "ga", "ning", "mi", "chi", "u", "у", "нас",
    "в", "по", "what", "что", "ҳозир", "қайси", "бор", "мошина", "машина", "рўйхат", "кўрсат", "aktiv", "faol", "актив",
    "active", "fleet", "park", "парк", "ta", "та", "info", "haqida", "about", "о", "об", "pls", "hozirda",
)
_COUNT = ("nechta", "necha", "qancha", "нечта", "неча", "қанча", "сколько", "how", "many", "total", "count", "soni",
          "statist", "статист", "stat", "umumiy", "умумий", "всего", "итого", "dashboard", "holat", "hisobot", "отчет",
          "отчёт", "summary", "overview", "ҳолат", "холат")
_PM = ("pm", "пм", "техобслуж", "texnik", "xizmat", "service", "moy", "moyi", "oil", "масл", "maintenance")
_PM_EXTRA = ("muddat", "o'tgan", "otgan", "o'tib", "otib", "ketgan", "yaqin", "qolgan", "qoldi", "kelgan", "overdue",
             "due", "soon", "просроч", "скоро", "близк", "gacha", "holat", "status", "статус", "состоян", "keyingi",
             "next", "almash", "замен", "vaqti", "time", "срок", "кечик", "ўтган", "муддат") + _COUNT
_CAB = ("cab", "card", "kab", "karta", "registr", "регистр", "кэб", "кеб")
_INSP = ("inspek", "inspection", "инспек", "техосмотр", "осмотр", "инспекц")
_EXP = _CAB + _INSP + ("hujjat", "документ", "document", "ҳужжат", "хужжат")
_EXP_EXTRA = ("muddat", "tugay", "tugag", "tugash", "o'tgan", "otgan", "o'tib", "otib", "ketgan", "expir", "истека",
              "истек", "kun", "kunda", "kunlik", "kunga", "kunlar", "day", "days", "дн", "дня", "дней", "oy", "oyda",
              "oylik", "month", "месяц", "hafta", "haftada", "week", "недел", "yaqin", "keyingi", "next", "следующ",
              "ichida", "within", "течение", "soon", "скоро", "срок", "qolgan", "holat", "status", "статус", "yangilash",
              "renew", "продл", "муддат", "тугай", "кун", "ой", "yillik", "annual", "годов") + _COUNT
_NO_DRIVER = re.compile(
    r"haydovchi(si)?\s*(yo'q|yoq|siz)|haydovchisiz|без\s+водител|no\s+drivers?|without\s+(a\s+)?drivers?|"
    r"unassigned|bo'sh\s+(turgan\s+)?(mashina|trak|truck|moshina)|ҳайдовчи(си)?\s*(йўқ|сиз)|хайдовчи(си)?\s*(йук|йўқ|сиз)"
)
_NO_DRIVER_EXTRA = ("haydovchi", "yo'q", "yoq", "bo'sh", "turgan", "turibdi", "qayerda", "где", "стоят", "where", "joy",
                    "joyi", "location", "без", "водител", "no", "driver", "without", "unassigned", "ҳайдовчи", "хайдовчи",
                    "йўқ", "қаерда", "каерда", "нет", "siz", "stand", "parked") + _COUNT
_CARD_EXTRA = ("kimda", "kim", "who", "кто", "кого", "haydaydi", "haydovchi", "driver", "drives", "водитель", "qayerda",
               "where", "где", "ma'lumot", "malumot", "информац", "details", "vin", "vini", "plate", "raqam", "nomer",
               "номер", "number", "muammo", "issue", "проблем", "gps", "joylashuv", "location", "yoqilg'i", "fuel",
               "топлив", "probeg", "пробег", "mileage", "karta", "карточк", "card", "ҳақида", "ким", "кимда", "қаерда",
               "mashinada", "trakda", "mashinasi", "trucki", "на", "ездит", "drive", "status", "holat", "статус", "pm",
               "inspek", "cab", "muddat", "egasi", "owner", "guruh", "group", "rang", "color")


def _norm(text: str) -> str:
    return text.translate(_APOS).lower().strip()


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(_norm(text))


def _has(tok: str, stems: tuple) -> bool:
    head = tok.split("'")[0]  # "pm'i" -> "pm"
    for s in stems:
        if len(s) <= 3:
            if tok == s or head == s:
                return True
        elif tok.startswith(s):
            return True
    return False


def _only(tokens: list[str], *vocabs: tuple) -> bool:
    return all(_has(t, _GENERIC) or any(_has(t, v) for v in vocabs) for t in tokens)


def _days(text: str) -> int | None:
    t = _norm(text)
    m = re.search(r"(\d{1,3})\s*(kun|day|дн|кун)", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,2})\s*(oy|month|месяц|ой)", t)
    if m:
        return int(m.group(1)) * 30
    if re.search(r"hafta|week|недел", t):
        return 7
    if re.search(r"(^|\s)(oy|oyda|month|месяц|ой)", t):
        return 30
    return None


class Router:
    def __init__(self, sql: SqlRunner):
        self.set_sql(sql)

    def set_sql(self, sql: SqlRunner) -> None:
        self.sql = sql
        conn = sql.data.conn
        self.units = {u.upper() for (u,) in conn.execute(
            "SELECT DISTINCT unit_number FROM all_trucks WHERE sheet != 'Inactive Trucks' AND unit_number IS NOT NULL")}
        # haydovchilar: to'liq ism va familiya (familiya faqat bitta odamga tegishli bo'lsa)
        names = [n for (n,) in conn.execute(
            "SELECT driver FROM all_trucks WHERE sheet IN ('TRUCK INFO', 'Rental trucks') AND driver IS NOT NULL "
            "UNION SELECT coworker FROM all_trucks WHERE sheet IN ('TRUCK INFO', 'Rental trucks') AND coworker IS NOT NULL")]
        vocab = _GENERIC + _COUNT + _PM_EXTRA + _EXP_EXTRA + _NO_DRIVER_EXTRA + _CARD_EXTRA + _PM + _EXP
        self.full_names: dict[tuple[str, ...], str] = {}
        surnames: dict[str, set[str]] = {}
        for n in names:
            toks = tuple(_tokens(n))
            if len(toks) >= 2:
                self.full_names[toks] = n
                surnames.setdefault(toks[-1], set()).add(n)
        self.surnames = {s: next(iter(v)) for s, v in surnames.items()
                         if len(v) == 1 and len(s) >= 4 and not _has(s, vocab)}

    def route(self, text: str) -> quick.Reply | None:
        m = UNIT_ONLY.match(text)
        if m and any(ch.isdigit() for ch in m.group(1)):  # faqat truck raqami / VIN / plate
            return quick.unit_lookup(self.sql, m.group(1))
        toks = _tokens(text)
        if not toks or len(toks) > 12:
            return None
        has_digit = any(t.isdigit() for t in toks)

        # 1) unit raqami gap ichida ("417 kimda", "9693 JCE haqida")
        unit, rest = self._find_unit(toks)
        if unit and _only(rest, _CARD_EXTRA):
            return quick.unit_lookup(self.sql, unit)
        # 2) haydovchi ismi ("John Smith qaysi mashinada")
        name, rest = self._find_driver(toks)
        if name and _only(rest, _CARD_EXTRA):
            rep = quick.driver_lookup(self.sql, name)
            if rep:
                return rep

        # 3) haydovchisiz mashinalar
        if _NO_DRIVER.search(_norm(text)) and not has_digit and _only(toks, _NO_DRIVER_EXTRA):
            return quick.no_driver(self.sql)
        # 4) PM holati
        if any(_has(t, _PM) for t in toks) and not has_digit and _only(toks, _PM, _PM_EXTRA):
            return quick.pm_report(self.sql)
        # 5) cab card / inspeksiya muddatlari (raqam faqat "60 kun" kabi bo'lishi mumkin)
        words = [t for t in toks if not t.isdigit()]
        if any(_has(t, _EXP) for t in toks) and _only(words, _EXP, _EXP_EXTRA) and (not has_digit or _days(text)):
            cab, insp = any(_has(t, _CAB) for t in toks), any(_has(t, _INSP) for t in toks)
            kinds = ("cab",) if cab and not insp else ("insp",) if insp and not cab else ("cab", "insp")
            return quick.expiring(self.sql, _days(text) or 30, kinds)
        # 6) umumiy statistika ("nechta mashina bor", "statistika")
        if any(_has(t, _COUNT) for t in toks) and not has_digit and _only(toks, _COUNT):
            return quick.stats(self.sql)
        return None

    def _find_unit(self, toks: list[str]) -> tuple[str | None, list[str]]:
        for i, t in enumerate(toks):
            if not t.isdigit():
                continue
            if i + 1 < len(toks) and f"{t} {toks[i + 1]}".upper() in self.units:  # '9693 JCE'
                return f"{t} {toks[i + 1]}", toks[:i] + toks[i + 2:]
            if t.upper() in self.units:
                return t, toks[:i] + toks[i + 1:]
        return None, toks

    def _find_driver(self, toks: list[str]) -> tuple[str | None, list[str]]:
        s = set(toks)
        best = None
        for parts, name in self.full_names.items():
            if parts[0] in s and parts[-1] in s and (best is None or len(parts) > len(best[0])):
                best = (parts, name)
        if best:
            return best[1], [t for t in toks if t not in best[0]]
        for t in toks:
            if t in self.surnames:
                return self.surnames[t], [x for x in toks if x != t]
        return None, toks
