"""Invoys maydonlarini Fleetio yozuvlariga moslash — tarmoqsiz, sof funksiyalar.

Vehicle (unit raqami), kontakt (haydovchi), label (Responsible), vendor (ustaxona) va service task ("For").
Qoidalar Fleetio'dagi 600 ta eski service entry asosida tanlangan: jamoa qo'lda qanday to'ldirgan bo'lsa shunday.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter

from .wo_parser import Invoice, Vendor

# ====================================================================== vehicle

def pick_target(inv: Invoice) -> tuple[str, str]:
    """Work order qaysi mashinaga: odatda Unit. Trailer qatori bo'lib, ish faqat treylerga tegishli bo'lsa — treyler
    (eski yozuvlarda "Trailer tire replacement" kabi ishlarning 85% i treylerga yozilgan, "Truck and trailer wash" esa unit'ga)."""
    p = inv.purpose.lower()
    about_trailer = bool(re.search(r"\b(?:trailers?|trl)\b", p)) and not re.search(r"\btrucks?\b", p)
    if inv.trailer and (not inv.unit or about_trailer):
        return inv.trailer, "trailer"
    return inv.unit, "unit"


def _vnorm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w ]+", " ", (s or "").upper())).strip()


def match_vehicle(unit: str, vehicles: list[dict]) -> tuple[dict | None, str]:
    """-> (vehicle, "") yoki (None, foydalanuvchiga inglizcha sabab). Nomlar: "3968", "1207 MM", "594118 RYL"."""
    want = _vnorm(unit)
    if not want:
        return None, "Unit number is missing."
    exact = [v for v in vehicles if _vnorm(v.get("name")) == want]
    if len(exact) == 1:
        return exact[0], ""
    m = re.search(r"\d{2,}", want)
    if not m:
        return None, f"Unit <b>{unit}</b> was not found in Fleetio."
    num = m.group()
    cands = [v for v in vehicles if re.match(rf"{num}\b", _vnorm(v.get("name")))]
    if not cands:  # "839" <-> "0839 CSA", yoki raqam nom o'rtasida
        cands = [v for v in vehicles
                 if any(t.isdigit() and int(t) == int(num) for t in _vnorm(v.get("name")).split())]
    if len(cands) > 1:
        suffix = [t for t in want.split() if not t.isdigit()]
        narrowed = [v for v in cands if suffix and all(t in _vnorm(v.get("name")).split() for t in suffix)]
        if len(narrowed) == 1:
            cands = narrowed
    if len(cands) == 1:
        return cands[0], ""
    if cands:
        names = ", ".join(str(v.get("name")) for v in cands[:8])
        return None, f"Unit <b>{unit}</b> matches several vehicles in Fleetio: {names}. Write the full unit name."
    return None, f"Unit <b>{unit}</b> was not found in Fleetio."


# ====================================================================== kontakt (haydovchi)

def _name_tokens(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z]+", s)


def match_contact(name: str, contacts: list[dict], prefer_id: int | None = None) -> dict | None:
    """'John Smith' -> kontakt 'John Michael Smith'. Noaniq bo'lsa None (taxmin qilinmaydi).

    prefer_id — shu mashinaga hozir biriktirilgan haydovchi: teng holatlarda ustun, bitta so'zli ism faqat shunga mos keladi."""
    want = _name_tokens(name)
    if not want:
        return None
    scored: list[tuple[float, dict, tuple]] = []
    for c in contacts:
        have = _name_tokens(c.get("name") or "")
        if not have:
            continue
        if all(t in have for t in want):
            score = 2.0 - 0.05 * (len(have) - len(want))  # o'rta ism kabi ortiqcha so'z kam bo'lgani yaxshi
        else:  # imlo xatosi: har so'z bosh harfi bir xil va juda o'xshash bo'lsa ("Reinaldo Suares")
            sims = [max((difflib.SequenceMatcher(None, t, h).ratio() for h in have if h[0] == t[0]), default=0.0)
                    for t in want]
            if len(want) < 2 or min(sims) < 0.8 or sum(sims) / len(sims) < 0.84:
                continue
            score = sum(sims) / len(sims)
        scored.append((score, c, tuple(sorted(have))))
    if not scored:
        return None
    best = max(s for s, _, _ in scored)
    top = [(c, toks) for s, c, toks in scored if best - s < 0.04]
    preferred = [c for c, _ in top if prefer_id is not None and c.get("id") == prefer_id]
    if preferred:
        return preferred[0]
    if len(want) == 1:
        return None
    if len({toks for _, toks in top}) > 1:
        return None  # har xil odamlar ("Juan Manuel Perez" / "Juan Luis Perez")
    # bir odamning takror kartochkalari: haydovchi deb belgilangani, keyin eng yangisi
    return max((c for c, _ in top), key=lambda c: (bool(c.get("vehicle_operator")), int(c.get("id") or 0)))


def same_person(a: str, b: str) -> bool:
    ta, tb = set(_name_tokens(a)), set(_name_tokens(b))
    if len(ta) < 2 or len(tb) < 2:
        return False
    return ta <= tb or tb <= ta or difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio() >= 0.88


# ====================================================================== label (Responsible)

_CANON_PREFIX = [(re.compile(r"(?i)^owner\b"), "Owner of the truck"),
                 (re.compile(r"(?i)^(?:company\w*|cacs?)\b"), "Company"),
                 (re.compile(r"(?i)^drivers?\b"), "Driver")]
_CANON_ANY = [(re.compile(r"(?i)\bowner\b"), "Owner of the truck"), (re.compile(r"(?i)\bcompany\b"), "Company"),
              (re.compile(r"(?i)\bdrivers?\b"), "Driver")]
MAX_NEW_LABEL_WORDS, MAX_NEW_LABEL_CHARS = 5, 40


def responsible_parts(text: str) -> list[str]:
    """'Company($38.60), Owner of the truck($42.50)' -> ['Company', 'Owner of the truck']."""
    t = re.sub(r"\([^)]*\)", " ", text or "")
    t = re.sub(r"(?i)(?:\bfor\s+)?\$\s*[\d.,]+|\b\d+[.,]\d+\b", " ", t)
    parts = [re.sub(r"\s+", " ", p).strip(" .,:;-") for p in re.split(r"\s*(?:,|&|\+|/|\band\b)\s*", t, flags=re.I)]
    return [p for p in parts if re.search(r"[A-Za-z]", p)]


def match_labels(responsible: str, labels: list[dict], drivers: list[str] | tuple = ()) -> tuple[list[dict], list[str], list[str]]:
    """Responsible matni -> (mavjud labellar, yaratiladigan yangi nomlar, labelga aylanmagan bo'laklar)."""
    by_low: dict[str, dict] = {}
    for lab in labels:
        by_low.setdefault((lab.get("name") or "").strip().lower(), lab)
    found, new, skipped, seen = [], [], [], set()

    def add(name: str) -> None:
        lab = by_low.get(name.lower())
        key = lab["id"] if lab else name.lower()
        if key in seen:
            return
        seen.add(key)
        if lab:
            found.append(lab)
        else:
            new.append(name)

    for part in responsible_parts(responsible):
        low = part.lower()
        if low in by_low:
            add(by_low[low]["name"])
            continue
        canon = next((name for pat, name in _CANON_PREFIX if pat.search(part)), None)
        if canon:
            add(canon)
            continue
        if any(same_person(part, d) for d in drivers):
            add("Driver")
            continue
        close = difflib.get_close_matches(low, list(by_low), n=1, cutoff=0.9)
        if close:
            add(by_low[close[0]]["name"])
        elif len(part.split()) <= MAX_NEW_LABEL_WORDS and len(part) <= MAX_NEW_LABEL_CHARS:
            add(part)
        else:
            canon = next((name for pat, name in _CANON_ANY if pat.search(part)), None)
            if canon:
                add(canon)
            else:
                skipped.append(part)
    return found, new, skipped


# ====================================================================== vendor (ustaxona)

_CORP = {"inc", "llc", "co", "corp", "ltd", "company", "incorporated", "the", "and", "of"}
# nomni ajratib bermaydigan umumiy so'zlar: "ABC Truck & Trailer Repair" != "Truck and Trailer Repair"
_GENERIC = {"truck", "trucks", "trailer", "trailers", "repair", "repairs", "tire", "tires", "service", "services",
            "shop", "center", "centre", "travel", "stop", "auto", "automotive", "diesel", "mobile", "road", "roadside",
            "towing", "tow", "recovery", "fleet", "commercial", "wash", "parts", "sales", "mechanic", "mechanical",
            "solutions", "express", "care", "plaza", "store", "at", "a", "24", "7", "hr", "hour"}


def _vname(s: str | None) -> str:
    s = re.sub(r"['’`]", "", (s or "").lower().replace("&", " and "))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _vtokens(s: str | None) -> list[str]:
    return [t for t in _vname(s).split() if t not in _CORP]


def _phone10(s: str | None) -> str:
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else ""


def _street_no(s: str | None) -> str:
    m = re.match(r"\s*(\d+)\b", s or "")
    return m.group(1) if m else ""


def _same_distinct(a: set[str], b: set[str]) -> bool:
    """Nomni ajratuvchi so'zlar ikkala tomonda bir xil (mayda imlo farqi bilan): {'tread','source'} ~ {'tred','source'}."""
    if not a or not b or len(a) != len(b):
        return False
    return all(any(x == y or (x[0] == y[0] and difflib.SequenceMatcher(None, x, y).ratio() >= 0.8) for y in b) for x in a)


def match_vendor(v: Vendor, vendors: list[dict]) -> dict | None:
    """Ustaxonani mavjud vendorlar ichidan topadi; topilmasa None (yangi yaratiladi).

    Nom 100% bir xil bo'lishi shart emas ("&"/"and", LLC, katta-kichik harf, mayda imlo farqi), lekin o'xshash nomli
    boshqa filialga (boshqa zip) ulanmaydi: TA Truck Service, Flying J kabi tarmoqlar har manzil uchun alohida vendor."""
    want_t = _vtokens(v.name)
    want = " ".join(want_t)
    if not want:
        return None
    wd = {t for t in want_t if t not in _GENERIC}
    vno = _street_no(v.street)
    best, best_rank = None, None
    for c in vendors:
        have_t = _vtokens(c.get("name"))
        have = " ".join(have_t)
        if not have:
            continue
        cphone, czip = _phone10(c.get("phone")), (c.get("postal_code") or "").strip()[:5]
        same_phone = bool(v.phone) and cphone == v.phone
        if have == want:
            sim = 1.0
        elif not same_phone and not set(want_t) & set(have_t):
            continue
        else:
            ratio = difflib.SequenceMatcher(None, want, have).ratio()
            cd = {t for t in have_t if t not in _GENERIC}
            nested = set(want_t) <= set(have_t) or set(have_t) <= set(want_t)
            if (ratio >= 0.9 or nested) and _same_distinct(wd, cd):
                sim = max(ratio, 0.88)
            elif same_phone and (wd & cd or ratio >= 0.6):
                sim = 0.8
            else:
                continue
        same_zip = bool(v.postal_code) and czip == v.postal_code
        if v.postal_code and czip and not same_zip and not same_phone:
            continue  # boshqa joydagi filial
        if sim < 1.0 and not (same_phone or same_zip) and (czip or cphone) and v.has_location:
            continue  # nom o'xshash, lekin manzil/telefon tasdiqlamadi
        same_no = bool(vno) and _street_no(c.get("street_address")) == vno
        rank = (3 if same_phone else 2 if (same_zip and same_no) else 1 if same_zip else 0, sim,
                bool(c.get("service")), -int(c.get("id") or 0))
        if best_rank is None or rank > best_rank:
            best, best_rank = c, rank
    return best


def same_name_vendor(v: Vendor, vendors: list[dict]) -> dict | None:
    """Aynan shu nomli vendor (manzilga qaramay) — Fleetio 'name already taken' desa shunga tushamiz."""
    want = " ".join(_vtokens(v.name))
    same = [c for c in vendors if " ".join(_vtokens(c.get("name"))) == want]
    same.sort(key=lambda c: (not c.get("service"), int(c.get("id") or 0)))
    return same[0] if same else None


def vendor_payload(v: Vendor) -> dict:
    body = {"name": v.name, "service": True, "street_address": v.street, "city": v.city, "region": v.region,
            "postal_code": v.postal_code, "country": v.country, "phone": v.phone_pretty}
    return {k: x for k, x in body.items() if x not in ("", None)}


# ====================================================================== service task ("For")

_TASK_DROP = {"truck", "trucks", "trailer", "trailers", "the", "a", "an", "of", "for", "on", "s", "new", "used"}


def _singular(t: str) -> str:
    if len(t) > 3 and t.endswith("es") and t[:-2].endswith(("sh", "ch", "x")):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def task_key(s: str | None) -> str:
    """'Truck tire repair.' / 'Trailer tires repair' / 'Tire Repair (s)' -> 'tire repair'."""
    s = re.sub(r"\(s\)", " ", (s or "").lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(_singular(t) for t in s.split() if t not in _TASK_DROP and not t.isdigit())


def _key_sim(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    # "turbo" ~ "turbocharger": 4+ harfli so'z ikkinchisining boshi bo'lsa ham mos deb olinadi
    hit = sum(1 for x in ta if x in tb or any(len(min(x, y, key=len)) >= 4 and (x.startswith(y) or y.startswith(x)) for y in tb))
    return 0.6 * hit / len(ta | tb) + 0.4 * difflib.SequenceMatcher(None, a, b).ratio()


def build_examples(entries: list[dict]) -> dict:
    """entries: [{"for": matn, "task_ids": [id, ...]}] -> examples fayli tarkibi.

    Bitta task'li yozuvlargina misol bo'ladi (ko'p task'li invoyslarda qaysi biri "asosiy"ligi noma'lum);
    task_usage esa hamma qatorlar bo'yicha."""
    usage: Counter = Counter()
    by_key: dict[str, Counter] = {}
    texts: dict[str, Counter] = {}
    used = 0
    for e in entries:
        ids = [i for i in e.get("task_ids") or [] if i]
        usage.update(ids)
        key = task_key(e.get("for"))
        if len(ids) != 1 or not key:
            continue
        used += 1
        by_key.setdefault(key, Counter())[ids[0]] += 1
        texts.setdefault(key, Counter())[" ".join(str(e["for"]).split()).strip(" .,")] += 1
    examples = [{"key": k, "for": texts[k].most_common(1)[0][0], "tasks": {str(i): n for i, n in c.most_common()}}
                for k, c in sorted(by_key.items(), key=lambda kv: -sum(kv[1].values()))]
    return {"entries": used, "task_usage": {str(i): n for i, n in usage.most_common()}, "examples": examples}


class TaskBook:
    """Service task katalogi + jamoaning eski tanlovlari (misollar) + tugma orqali o'rgatilgan mosliklar."""

    AUTO_MIN_COUNT, AUTO_MIN_SHARE = 2, 0.6

    def __init__(self, tasks: list[dict], examples: dict | None = None, aliases: dict | None = None):
        self.by_id: dict[int, dict] = {int(t["id"]): t for t in tasks}
        ex = examples or {}
        self.usage: Counter = Counter({int(i): n for i, n in (ex.get("task_usage") or {}).items() if int(i) in self.by_id})
        self.examples: dict[str, Counter] = {}
        self.example_text: dict[str, str] = {}
        for e in ex.get("examples") or []:
            c = Counter({int(i): n for i, n in e["tasks"].items() if int(i) in self.by_id})
            if c:
                self.examples[e["key"]] = c
                self.example_text[e["key"]] = e.get("for") or e["key"]
        self.aliases: dict[str, int] = {k: int(i) for k, i in (aliases or {}).items() if int(i) in self.by_id}

    def task(self, task_id) -> dict | None:
        try:
            return self.by_id.get(int(task_id))
        except (TypeError, ValueError):
            return None

    def local(self, purpose: str) -> tuple[dict | None, str]:
        """LLM'siz aniq hal bo'ladigan holatlar: o'rgatilgan moslik yoki eski yozuvlarda bir xil tanlangan task."""
        key = task_key(purpose)
        if key in self.aliases:
            return self.by_id[self.aliases[key]], "learned"
        c = self.examples.get(key)
        if c:
            (top, n), total = c.most_common(1)[0], sum(c.values())
            if total >= self.AUTO_MIN_COUNT and n / total >= self.AUTO_MIN_SHARE:
                return self.by_id[top], "history"
        return None, ""

    def similar_examples(self, purpose: str, n: int = 15) -> list[tuple[str, int, int]]:
        """-> [(eski "For" matni, task id, necha marta)] — eng o'xshashlari."""
        key = task_key(purpose)
        scored = sorted(((_key_sim(key, k), k) for k in self.examples), reverse=True)
        out = []
        for sim, k in scored[:n]:
            if sim < 0.15:
                break
            top, cnt = self.examples[k].most_common(1)[0]
            out.append((self.example_text[k], top, cnt))
        return out

    def team_tasks(self, n: int = 180) -> list[tuple[int, int]]:
        return self.usage.most_common(n)

    def catalog_matches(self, purpose: str, n: int = 20) -> list[int]:
        """Jamoa hali ishlatmagan, lekin nomi mos tasklar: faqat Fleetio standartlari va "(s)" bilan belgilanganlari
        (bir martalik tavsif ko'rinishidagi tasklar — "Trailer 44025 tire replacement" — chiqarilmaydi)."""
        key = task_key(purpose)
        scored = []
        for i, t in self.by_id.items():
            if i in self.usage or not (t.get("standard") or t["name"].rstrip().endswith("(s)")):
                continue
            sim = _key_sim(key, task_key(t["name"]))
            if sim >= 0.3:
                scored.append((sim, i))
        return [i for _, i in sorted(scored, reverse=True)[:n]]

    def options(self, purpose: str, extra: list[int] | tuple = (), n: int = 6) -> list[dict]:
        """Tugmalar uchun nomzodlar: avval extra (LLM taklifi), keyin o'xshash misollar va nomi mos tasklar."""
        ids: list[int] = [i for i in extra if i in self.by_id]
        ids += [tid for _, tid, _ in self.similar_examples(purpose, 8)]
        key = task_key(purpose)
        named = sorted(((_key_sim(key, task_key(self.by_id[i]["name"])), self.usage[i], i) for i in self.usage), reverse=True)
        ids += [i for sim, _, i in named[:6] if sim >= 0.3]
        ids += self.catalog_matches(purpose, 4)
        out, seen = [], set()
        for i in ids:
            if i not in seen:
                seen.add(i)
                out.append(self.by_id[i])
        return out[:n]
