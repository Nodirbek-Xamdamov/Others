"""Tugma/buyruq hisobotlari — LLM'siz, to'g'ridan-to'g'ri Fleetio API'dan (faqat o'qish).

LLM orqali bu hisobotlar 10-60 s olardi: model yuzlab yozuvni sahifalab o'qib, nomlarni alohida so'rardi
(muddati o'tgan service reminder'lar ~1300 ta). Bu yerda kerakli ro'yxatlar parallel olinadi va Python aniq sanab,
ixcham yozadi. Og'ir hisobotlar (service, renewals) fonda har PREFETCH_EVERY_S da tayyorlanadi — tugma bosilganda
darhol chiqadi; qolganlari 0-1 s (mashinalar ro'yxati keshda, issues bitta so'rov).

Hisobotlar: overview, service, renewals, issues, nodriver, unit (bitta mashina kartasi).
Mashinalar ro'yxati umumiy keshdan olinadi (work order ma'lumotnomasi bilan bitta — fonda yangilanib turadi).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import fmt
from . import wo_match as M
from .fleetio import FleetioClient, web_link

log = logging.getLogger("reports")

# hisobot keshi (soniya): shu muddat ichida qayta bosilsa — darhol. service/renewals fonda har PREFETCH_EVERY_S da
# yangilanadi (service ~1300 yozuv = 14 sahifa, 7-9 s — tugma bosilganda kutilmasin)
REPORT_TTL_S = {"service": 900, "renewals": 900, "issues": 120}
PREFETCH = ("service", "renewals")
PREFETCH_EVERY_S = 600
VEHICLES_TTL_S = 300        # mashinalar ro'yxati (alohida kesh berilmaganda)
TYPES_TTL_S = 6 * 3600
MAX_PAGES = 30              # ko'pi bilan 3000 yozuv (muddati o'tgan service reminder'lar ~1300 ta: ~7 s)
MAX_LINES = 25              # bitta bo'limdagi qatorlar
MAX_UNITS = 40              # bitta qatordagi mashina nomlari

NAMES = ("overview", "service", "renewals", "issues", "nodriver", "unit")

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="report")


def _unit(rec: dict) -> str:
    """Yozuvdagi mashina (yoki uskuna) nomi."""
    for k in ("vehicle", "equipment"):
        name = (rec.get(k) or {}).get("name")
        if name:
            return str(name)
    return str(rec.get("vehicle_name") or rec.get("vehicle_id") or "?")


def _date(s) -> str:
    return fmt.date(str(s)[:10], mark=False) if s else ""


def _meter(v, unit: str = "mi") -> str:
    try:
        return f"{float(v):,.0f} {unit}"
    except (TypeError, ValueError):
        return ""


def _is_trailer(v: dict) -> bool:
    t = (v.get("vehicle_type_name") or "").lower()
    return "trailer" in t and "truck" not in t


def _more(n: int) -> str:
    return f"… and {n} more" if n > 0 else ""


def _count(recs: list) -> str:
    """Sahifa chegarasiga yetgan bo'lsa son aniq emas: '3000+'."""
    return f"{len(recs)}+" if len(recs) >= MAX_PAGES * 100 else str(len(recs))


def _units_line(names: list[str]) -> str:
    shown = ", ".join(fmt.esc(x) for x in names[:MAX_UNITS])
    return shown + (f" … +{len(names) - MAX_UNITS}" if len(names) > MAX_UNITS else "")


class _Ttl:
    """Kichik kesh: qiymat ttl soniya amal qiladi; yuklash bir vaqtda faqat bitta thread'da."""

    def __init__(self, loader: Callable[[], object], ttl: float):
        self.loader, self.ttl = loader, ttl
        self.value, self.at = None, 0.0
        self.lock = threading.Lock()

    def get(self, fresh: bool = False):
        with self.lock:
            if self.value is None or fresh or time.monotonic() - self.at > self.ttl:
                self.value, self.at = self.loader(), time.monotonic()
            return self.value


class Reports:
    def __init__(self, client: FleetioClient, vehicles: Callable[..., list[dict]] | None = None):
        """vehicles(fresh=False) — mashinalar ro'yxati (to'liq yozuvlar); berilmasa o'zi keshlaydi."""
        self.client = client
        if vehicles is None:
            own = _Ttl(lambda: client.raw_all("list_vehicles", max_pages=MAX_PAGES), VEHICLES_TTL_S)
            vehicles = own.get
        self._vehicles = vehicles
        self._types = _Ttl(lambda: {t["id"]: t.get("name") or "" for t in client.raw_all("list_vehicle_renewal_types")},
                           TYPES_TTL_S)
        self._cache: dict[tuple, tuple[float, str]] = {}
        self._locks: dict[tuple, threading.Lock] = {}

    # ------------------------------------------------------------------ umumiy
    def get(self, name: str, arg: str | None = None, fresh: bool = False) -> str:
        """Hisobot matni (Telegram HTML). Fleetio xatosi FleetioError bo'lib chiqadi."""
        key = (name, (arg or "").strip().lower())
        ttl = REPORT_TTL_S.get(name, 0)  # overview/nodriver/unit — mashinalar keshidan, o'zi darhol
        # ikki kishi birdan bossa — ikkinchisi birinchisining natijasini kutib oladi (Fleetio'ga ikki marta bormaymiz)
        with self._locks.setdefault(key, threading.Lock()):
            hit = self._cache.get(key)
            if hit and not fresh:
                age = time.monotonic() - hit[0]
                if age < ttl:
                    return hit[1] + (f"\n\n<i>Fleetio data from {int(age // 60)} min ago.</i>" if age >= 120 else "")
            t0 = time.monotonic()
            text = getattr(self, f"_r_{name}")(arg) if name in NAMES else "Unknown report."
            log.info("hisobot %s%s: %.2f s", name, f" {arg!r}" if arg else "", time.monotonic() - t0)
            if ttl:
                self._cache[key] = (time.monotonic(), text)
            return text

    def prefetch(self) -> None:
        """Fonda: og'ir hisobotlarni oldindan tayyorlaydi (tugma bosilganda darhol chiqishi uchun)."""
        for name in PREFETCH:
            try:
                self.get(name, fresh=True)
            except Exception as e:  # noqa: BLE001 — keyingi safar yana; bosilganda o'zi hisoblanadi
                log.warning("Hisobotni oldindan tayyorlab bo'lmadi (%s): %s", name, e)

    def _all(self, tool: str, params: dict | None = None) -> list[dict]:
        return self.client.raw_all(tool, params, max_pages=MAX_PAGES)

    def _both(self, tool: str, field: str) -> tuple[list[dict], list[dict]]:
        """overdue va due_soon ro'yxatlari parallel (sahifalash ketma-ket, shuning uchun ikkita zanjir birga)."""
        futs = [_pool.submit(self._all, tool, {"filter": {field: {"eq": st}}}) for st in ("overdue", "due_soon")]
        res = [f.result() for f in futs]
        live = [[r for r in recs if r.get("active", True) is not False and not r.get("vehicle_archived_at")] for recs in res]
        return live[0], live[1]

    def _vehicle_map(self) -> dict:
        try:
            return {v["id"]: v for v in self._vehicles()}
        except Exception as e:  # noqa: BLE001 — hisoblagich qiymati qo'shimcha, ularsiz ham hisobot chiqadi
            log.warning("Mashinalar ro'yxati olinmadi: %s", e)
            return {}

    # ------------------------------------------------------------------ hisobotlar
    def _r_overview(self, _arg=None) -> str:
        veh = self._vehicles()
        by_status = Counter(v.get("vehicle_status_name") or "(no status)" for v in veh)
        by_type = Counter(v.get("vehicle_type_name") or "(no type)" for v in veh)
        free = sum(1 for v in veh if not v.get("driver") and not _is_trailer(v))
        out = [f"🚚 <b>Fleet overview</b> — {len(veh)} vehicles", ""]
        out += [f"• {fmt.esc(s)}: <b>{n}</b>" for s, n in by_status.most_common()]
        out += ["", "<b>By type:</b> " + " · ".join(f"{fmt.esc(t)} {n}" for t, n in by_type.most_common()),
                f"🅿️ Trucks without a driver: <b>{free}</b>"]
        return "\n".join(out)

    def _r_nodriver(self, _arg=None) -> str:
        trucks = [v for v in self._vehicles() if not v.get("driver") and not _is_trailer(v)]
        if not trucks:
            return "✅ Every truck has a driver assigned in Fleetio."
        groups: dict[str, list[str]] = defaultdict(list)
        for v in trucks:
            groups[v.get("vehicle_status_name") or "(no status)"].append(str(v.get("name")))
        out = [f"🅿️ <b>Trucks without a driver</b> — {len(trucks)}", "<i>Trailers are not counted.</i>"]
        for status, names in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            out += ["", f"<b>{fmt.esc(status)}</b> ({len(names)}): {_units_line(sorted(names))}"]
        return "\n".join(out)

    def _r_service(self, _arg=None) -> str:
        overdue, soon = self._both("list_service_reminders", "service_reminder_status")
        vmap = self._vehicle_map()

        def due(r: dict) -> str:
            bits = []
            if r.get("next_due_at"):
                bits.append(_date(r["next_due_at"]))
            m = _meter(r.get("next_due_primary_meter_value"))
            if m:
                cur = _meter((vmap.get(r.get("vehicle_id")) or {}).get("primary_meter_value"))
                bits.append(f"at {m}" + (f" (now {cur})" if cur else ""))
            return " · ".join(bits)

        task = lambda r: (r.get("service_task") or {}).get("name") or "Service"  # noqa: E731
        if not overdue and not soon:
            return "✅ No overdue or due-soon service reminders in Fleetio."
        n_veh = len({r.get("vehicle_id") for r in overdue})
        out = ["🛠 <b>Service reminders</b>",
               f"❗️ Overdue: <b>{_count(overdue)}</b> on {n_veh} vehicles · ⚠️ Due soon: <b>{_count(soon)}</b>"]
        if soon:
            soon.sort(key=lambda r: (r.get("next_due_at") or "9999", _unit(r)))
            out += ["", "⚠️ <b>Due soon</b>"]
            out += [f"• <b>{fmt.esc(_unit(r))}</b> — {fmt.esc(task(r))}" + (f" · {due(r)}" if due(r) else "")
                    for r in soon[:MAX_LINES]]
            out += [_more(len(soon) - MAX_LINES)] if len(soon) > MAX_LINES else []
        if overdue:
            per: dict[str, list[str]] = defaultdict(list)
            for r in overdue:
                per[_unit(r)].append(task(r))
            out += ["", "❗️ <b>Vehicles with the most overdue reminders</b>"]
            top = sorted(per.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            for unit, tasks in top[:15]:
                names = ", ".join(fmt.esc(t) for t in tasks[:3]) + ("…" if len(tasks) > 3 else "")
                out.append(f"• <b>{fmt.esc(unit)}</b> — {len(tasks)}: {names}")
            out += [_more(len(top) - 15)] if len(top) > 15 else []
            out += ["", "❗️ <b>Overdue by service task</b>"]
            out += [f"• {fmt.esc(t)} — {n}" for t, n in Counter(task(r) for r in overdue).most_common(10)]
        return "\n".join(out)

    def _r_renewals(self, _arg=None) -> str:
        futs = _pool.submit(self._types.get)
        overdue, soon = self._both("list_vehicle_renewal_reminders", "vehicle_renewal_reminder_status")
        try:
            types = futs.result()
        except Exception as e:  # noqa: BLE001 — turi nomisiz ham chiqadi
            log.warning("Renewal turlari olinmadi: %s", e)
            types = {}
        kind = lambda r: types.get(r.get("vehicle_renewal_type_id")) or "Renewal"  # noqa: E731
        if not overdue and not soon:
            return "✅ No overdue or due-soon renewals in Fleetio."
        out = ["📄 <b>Renewal reminders</b>", f"❗️ Overdue: <b>{_count(overdue)}</b> · ⚠️ Due soon: <b>{_count(soon)}</b>"]
        if soon:
            soon.sort(key=lambda r: (r.get("next_due_at") or "9999", _unit(r)))
            out += ["", "⚠️ <b>Due soon</b>"]
            out += [f"• <b>{fmt.esc(_unit(r))}</b> — {fmt.esc(kind(r))} · {_date(r.get('next_due_at'))}"
                    for r in soon[:MAX_LINES]]
            out += [_more(len(soon) - MAX_LINES)] if len(soon) > MAX_LINES else []
        if overdue:
            per: dict[str, list[dict]] = defaultdict(list)
            for r in overdue:
                per[kind(r)].append(r)
            out += ["", "❗️ <b>Overdue</b>"]
            for name, recs in sorted(per.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                units = sorted({_unit(r) for r in recs}, key=lambda u: (len(u), u))
                out.append(f"• <b>{fmt.esc(name)}</b> ({len(recs)}): {_units_line(units)}")
        return "\n".join(out)

    def _r_issues(self, _arg=None) -> str:
        issues = self._all("list_issues", {"filter": {"state": {"eq": "Open"}}, "sort": {"reported_at": "desc"}})
        if not issues:
            return "✅ No open issues in Fleetio."
        out = [f"⚠️ <b>Open issues</b> — {len(issues)}"]
        for x in issues[:MAX_LINES * 2]:
            prio = (x.get("issue_priority") or {}).get("name") or ""
            bits = [f"#{fmt.esc(str(x.get('number') or '').lstrip('#'))} {fmt.esc(x.get('summary') or x.get('name') or '')}".strip()]
            if x.get("reported_at"):
                bits.append(_date(x["reported_at"]))
            if prio and prio.lower() != "no priority":
                bits.append(fmt.esc(prio))
            if x.get("overdue"):
                bits.append("❗️ overdue")
            out.append(f"• <b>{fmt.esc(_unit(x))}</b> — " + " · ".join(bits))
        out += [_more(len(issues) - MAX_LINES * 2)] if len(issues) > MAX_LINES * 2 else []
        return "\n".join(out)

    def _r_unit(self, arg=None) -> str:
        want = (arg or "").strip()
        if not want:
            return "Send the unit number, for example: <code>/unit 417</code>"
        v, why = M.match_vehicle(want, self._vehicles())
        if v is None:  # yaqinda qo'shilgan bo'lishi mumkin
            v, why = M.match_vehicle(want, self._vehicles(fresh=True))
        if v is None:
            return "🔎 " + why
        e = fmt.esc
        head = " · ".join(e(x) for x in (v.get("vehicle_status_name"), v.get("vehicle_type_name")) if x)
        out = [f"🚛 <b>{e(v.get('name'))}</b>" + (f" · {head}" if head else "")]
        driver = (v.get("driver") or {}).get("name")
        out.append(f"👤 Driver: <b>{e(driver)}</b>" if driver else "👤 No driver assigned")
        year = v.get("year")
        year = str(int(year)) if isinstance(year, (int, float)) and float(year).is_integer() else (year or "")
        ymm = " ".join(str(x) for x in (year, v.get("make"), v.get("model"), v.get("trim")) if x)
        if ymm:
            out.append(f"🚚 {e(ymm)}")
        if v.get("vin"):
            out.append(f"🔢 VIN: <code>{e(v['vin'])}</code>")
        if v.get("license_plate"):
            state = f" ({e(v['registration_state'])})" if v.get("registration_state") else ""
            out.append(f"🪪 Plate: {e(v['license_plate'])}{state}")
        meter = _meter(v.get("primary_meter_value"), v.get("primary_meter_unit") or "mi")
        if meter:
            when = _date(v.get("primary_meter_date"))
            out.append(f"📟 {meter}" + (f" · {when}" if when else ""))
        if v.get("group_name"):
            out.append(f"🏷 Group: {e(v['group_name'])}")
        out.append(f'🔗 <a href="{web_link("vehicles", v["id"])}">Open in Fleetio</a>')
        return "\n".join(out)
