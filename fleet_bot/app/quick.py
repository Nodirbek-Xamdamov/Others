"""LLM'siz tezkor javoblar (millisekundda ishlaydi, raqamlar to'g'ridan-to'g'ri SQLite'dan).

Hamma funksiyalar Telegram HTML matn (ingliz tilida) + inline tugmalar uchun unit raqamlarini qaytaradi.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .agent import SqlRunner
from .fmt import PM_SOON, date, esc, num, pm, today


@dataclass
class Reply:
    text: str                                        # Telegram HTML
    units: list[str] = field(default_factory=list)   # tagida "🚛 417" tugmalari chiqadi


_SHEET_LABEL = {
    "TRUCK INFO": "🟢 Active fleet",
    "CAC Trucks": "🔵 CAC list",
    "DEMIR Trucks": "🔵 DEMIR list",
    "New trucks": "🆕 New trucks list",
    "Rental trucks": "🔑 Rental",
    "Inactive Trucks": "⚪️ Inactive (archive)",
}
_SHEET_WHERE = {
    "TRUCK INFO": "Active fleet",
    "CAC Trucks": "CAC list",
    "DEMIR Trucks": "DEMIR list",
    "New trucks": "New trucks list",
    "Rental trucks": "Rental list",
    "Inactive Trucks": "Inactive archive",
}
_TRUCK_COLS = ("sheet, unit_number, driver, coworker, location_note, vehicle_group, owner, color, make, model, year, "
               "vin, plate_number, cab_card_state, cab_card_expiration, inspection_expiration, current_mileage, miles_until_pm")
_SHEET_ORDER = (" ORDER BY CASE sheet WHEN 'TRUCK INFO' THEN 0 WHEN 'Rental trucks' THEN 1 "
                "WHEN 'Inactive Trucks' THEN 3 ELSE 2 END, unit_number")


def _iso_today() -> str:
    return dt.date.today().isoformat()


def _who(d: dict) -> str:
    if d["driver"]:
        return f"👤 <b>{esc(d['driver'])}</b>" + (f" + {esc(d['coworker'])}" if d["coworker"] else "")
    return "🅿️ No driver" + (f" — {esc(d['location_note'])}" if d["location_note"] else "")


def _group_trucks(rows: list[dict]) -> list[list[dict]]:
    """Bir mashinaning turli varaqlardagi yozuvlarini VIN bo'yicha birlashtiradi (VIN bo'sh bo'lsa unit bo'yicha)."""
    groups: dict[str, list[dict]] = {}
    unit_to_key: dict[str, str] = {}
    for d in rows:
        unit = (d["unit_number"] or "").upper()
        key = (d["vin"] or "").upper() or unit_to_key.get(unit) or unit
        groups.setdefault(key, []).append(d)
        unit_to_key.setdefault(unit, key)
    return list(groups.values())


def _fetch(sql: SqlRunner, where: str, params: tuple) -> list[dict]:
    res = sql.run(f"SELECT {_TRUCK_COLS} FROM all_trucks WHERE {where}{_SHEET_ORDER}", params)
    return [dict(zip(res["columns"], r)) for r in res.get("rows", [])]


def _cards_or_list(sql: SqlRunner, groups: list[list[dict]], q: str) -> Reply:
    if len(groups) <= 3:  # 1-3 ta mashina -> har biriga to'liq karta
        return Reply("\n\n".join(_card(sql, g) for g in groups))
    lines = [f"🔎 <b>{len(groups)} trucks match “{esc(q)}”</b>", "Tap one to open its card:", ""]
    for g in groups[:20]:
        d = g[0]
        who = d["driver"] or d["location_note"] or "no driver"
        vin = f" · VIN …{esc(d['vin'][-4:])}" if d["vin"] else ""
        lines.append(f"• <b>{esc(d['unit_number'])}</b> · {esc(who)}{vin} · {_SHEET_LABEL.get(d['sheet'], esc(d['sheet']))}")
    if len(groups) > 20:
        lines.append(f"… and {len(groups) - 20} more")
    return Reply("\n".join(lines), [g[0]["unit_number"] for g in groups[:20]])


def unit_lookup(sql: SqlRunner, q: str) -> Reply:
    """Unit raqami, plate yoki VIN oxiri bo'yicha mashina kartasi."""
    q = q.strip()
    s = _safe(q).strip()  # faqat harf/raqam/probel/tire qoladi
    if not s:
        return Reply("Type a truck number, e.g. <b>417</b>")
    # 1) aniq unit ('417' yoki '417 XYZ') / plate, 2) VIN oxiri yoki qisman moslik
    rows = _fetch(sql, "unit_number = ? COLLATE NOCASE OR unit_number LIKE ? OR plate_number = ? COLLATE NOCASE", (s, s + " %", s))
    if not rows:
        rows = _fetch(sql, "vin LIKE ? OR unit_number LIKE ? OR plate_number LIKE ?", ("%" + s, "%" + s + "%", "%" + s + "%"))
    if not rows:
        return Reply(f"🔎 No truck found for “{esc(q)}”.\nTry the unit number, plate, or the last 4–6 characters of the VIN.")
    return _cards_or_list(sql, _group_trucks(rows), q)


def driver_lookup(sql: SqlRunner, name: str) -> Reply | None:
    """Haydovchi ismi bo'yicha u haydayotgan mashina(lar). Topilmasa None (savol LLM'ga o'tadi)."""
    s = name.strip()
    if len(s) < 3:
        return None
    rows = _fetch(sql, "driver LIKE ? OR coworker LIKE ?", (f"%{s}%", f"%{s}%"))
    if not rows:
        return None
    groups = _group_trucks(rows)
    active = [g for g in groups if any(d["sheet"] != "Inactive Trucks" for d in g)]
    archived = len(groups) - len(active)
    rep = _cards_or_list(sql, active or groups, s)
    if active and archived:
        rep.text += f"\n\n<i>ℹ️ Also listed as last driver of {archived} truck(s) in the inactive archive.</i>"
    return rep


def _card(sql: SqlRunner, group: list[dict]) -> str:
    d, others = group[0], group[1:]
    title = " ".join(str(x) for x in (d["make"], d["model"], d["year"]) if x)
    lines = [f"🚛 <b>Unit {esc(d['unit_number'])}</b>" + (f" — {esc(title)}" if title else "")]
    meta = [_SHEET_LABEL.get(d["sheet"], esc(d["sheet"]))] + [esc(x) for x in (d["vehicle_group"], d["color"]) if x]
    lines.append(" · ".join(meta))
    lines.append(_who(d))
    ids = []
    if d["vin"]:
        ids.append(f"VIN <code>{esc(d['vin'])}</code>")
    if d["plate_number"]:
        ids.append(f"Plate <code>{esc(d['plate_number'])}</code>" + (f" ({esc(d['cab_card_state'])})" if d["cab_card_state"] else ""))
    if ids:
        lines.append("🔢 " + " · ".join(ids))
    if d["cab_card_expiration"] or d["inspection_expiration"]:
        lines.append(f"📄 Cab card: {date(d['cab_card_expiration'])} · Inspection: {date(d['inspection_expiration'])}")
    if d["current_mileage"] or d["miles_until_pm"] is not None:
        parts = [f"Mileage {num(d['current_mileage'])} mi"] if d["current_mileage"] else []
        if d["miles_until_pm"] is not None:
            parts.append(pm(d["miles_until_pm"]))
        lines.append("🛠 " + " · ".join(parts))

    gps = sql.run("SELECT location, last_connected, last_trip, engine_status, fuel_level_pct FROM gps_tracking "
                  "WHERE unit_number = ? COLLATE NOCASE OR (vin IS NOT NULL AND vin = ?) LIMIT 1",
                  (d["unit_number"], d["vin"] or ""))
    if gps.get("rows"):
        loc, conn, trip, eng, fuel = gps["rows"][0]
        g = [esc(x) for x in (loc, "on trip" if trip == "On trip" else None,
                              f"engine {eng}" if eng else None,
                              f"fuel {fuel:.0f}%" if fuel is not None else None) if x]
        lines.append("🛰 GPS: " + " · ".join(g) + (f" <i>(last signal {date(conn[:10], mark=False)})</i>" if conn else ""))

    iss = sql.run("SELECT recent_issue FROM recent_issues WHERE recent_issue IS NOT NULL AND "
                  "(unit_number = ? COLLATE NOCASE OR (vin IS NOT NULL AND vin = ?)) LIMIT 1",
                  (d["unit_number"], d["vin"] or ""))
    if iss.get("rows"):
        lines.append(f"🔧 Last issue: {esc(iss['rows'][0][0])}")

    seen = {d["sheet"]}
    for o in others:  # boshqa varaqlardagi yozuvlar: har varaqdan bittasi, faqat farqlari
        if o["sheet"] in seen:
            continue
        seen.add(o["sheet"])
        diff = []
        if (o["driver"] or o["location_note"]) != (d["driver"] or d["location_note"]):
            diff.append(f"driver {esc(o['driver'])}" if o["driver"] else "status: " + esc(o["location_note"] or "no driver"))
        if o["plate_number"] and o["plate_number"] != d["plate_number"]:
            diff.append(f"plate {esc(o['plate_number'])}")
        for col, label in (("cab_card_expiration", "cab card"), ("inspection_expiration", "inspection")):
            if o[col] and o[col] != d[col]:
                diff.append(f"{label} {date(o[col])}")
        where = _SHEET_WHERE.get(o["sheet"], f"{esc(o['sheet'])} sheet")
        lines.append(f"<i>ℹ️ Also in {where}" + (": " + ", ".join(diff) if diff else "") + "</i>")
    return "\n".join(lines)


def pm_report(sql: SqlRunner, threshold: int = PM_SOON) -> Reply:
    rows = sql.run(
        "SELECT unit_number, COALESCE(driver, location_note, '—'), miles_until_pm FROM trucks "
        "WHERE miles_until_pm IS NOT NULL AND miles_until_pm < ? ORDER BY miles_until_pm", (threshold,)
    ).get("rows", [])
    missing = sql.run("SELECT COUNT(*) FROM trucks WHERE miles_until_pm IS NULL")["rows"][0][0]
    over = [r for r in rows if r[2] < 0]
    soon = [r for r in rows if r[2] >= 0]
    out = ["🛠 <b>PM status</b> — active fleet", ""]
    if over:
        out.append(f"❗️ <b>Overdue: {len(over)}</b>")
        out += [f"• <b>{esc(u)}</b> · {esc(d)} · {num(-m)} mi over" for u, d, m in over]
    else:
        out.append("✅ No trucks with overdue PM")
    out.append("")
    if soon:
        out.append(f"⚠️ <b>Due within {num(threshold)} mi: {len(soon)}</b>")
        out += [f"• <b>{esc(u)}</b> · {esc(d)} · {num(m)} mi left" for u, d, m in soon]
    else:
        out.append(f"✅ No trucks due within {num(threshold)} mi")
    if missing:
        out += ["", f"<i>ℹ️ {missing} trucks have incomplete PM data (mileage or next PM missing).</i>"]
    return Reply("\n".join(out).strip(), [r[0] for r in rows][:20])


_DOCS = {"cab": ("cab_card_expiration", "Cab card (registration)"), "insp": ("inspection_expiration", "Annual inspection")}


def expiring(sql: SqlRunner, days: int = 30, kinds: tuple = ("cab", "insp")) -> Reply:
    days = max(1, min(days, 365))
    out = [f"📄 <b>Document expirations</b> — expired or expiring within {days} days", ""]
    units: list[str] = []
    for col, label in (_DOCS[k] for k in kinds):
        rows = sql.run(
            f"SELECT unit_number, COALESCE(driver, location_note, '—'), {col} FROM trucks "
            f"WHERE {col} IS NOT NULL AND {col} <= date(?, '+' || ? || ' day') ORDER BY {col} LIMIT 40",
            (_iso_today(), days),
        ).get("rows", [])
        if rows:
            expired = sum(1 for r in rows if r[2] < _iso_today())
            out.append(f"<b>{label}: {len(rows)}</b>" + (f" (❗️ {expired} expired)" if expired else ""))
            out += [f"• <b>{esc(u)}</b> · {esc(d)} · {date(e)}" for u, d, e in rows]
            units += [r[0] for r in rows]
        else:
            out.append(f"✅ <b>{label}</b>: nothing expiring within {days} days")
        out.append("")
    return Reply("\n".join(out).strip(), list(dict.fromkeys(units))[:20])


def no_driver(sql: SqlRunner) -> Reply:
    rows = sql.run(
        "SELECT COALESCE(location_note, 'Location not set') loc, unit_number FROM trucks WHERE driver IS NULL "
        "ORDER BY loc, unit_number"
    ).get("rows", [])
    if not rows:
        return Reply("✅ Every truck in the active fleet has a driver.")
    by_loc: dict[str, list[str]] = {}
    for loc, unit in rows:
        by_loc.setdefault(loc, []).append(unit)
    out = [f"🅿️ <b>Trucks without a driver: {len(rows)}</b> (active fleet)", ""]
    for loc, units in sorted(by_loc.items(), key=lambda x: -len(x[1])):
        out.append(f"<b>{esc(loc)}</b> ({len(units)}): " + ", ".join(esc(u) for u in units))
    return Reply("\n".join(out))


def stats(sql: SqlRunner) -> Reply:
    def one(q: str, params: tuple = ()):
        return sql.run(q, params)["rows"][0]

    iso = _iso_today()
    total, with_d = one("SELECT COUNT(*), SUM(driver IS NOT NULL) FROM trucks")
    with_d = with_d or 0
    rental = one("SELECT COUNT(*) FROM rental_trucks")[0]
    pm_over, pm_soon = one("SELECT SUM(miles_until_pm < 0), SUM(miles_until_pm BETWEEN 0 AND ?) FROM trucks", (PM_SOON,))
    cab_x, insp_x, cab_s, insp_s = one(
        "SELECT SUM(cab_card_expiration < ?1), SUM(inspection_expiration < ?1), "
        "SUM(cab_card_expiration BETWEEN ?1 AND date(?1, '+30 day')), "
        "SUM(inspection_expiration BETWEEN ?1 AND date(?1, '+30 day')) FROM trucks", (iso,))
    groups = sql.run("SELECT COALESCE(vehicle_group, '—'), COUNT(*) FROM trucks GROUP BY 1 ORDER BY 2 DESC")["rows"]
    makes = sql.run("SELECT make, COUNT(*) FROM trucks WHERE make IS NOT NULL GROUP BY 1 ORDER BY 2 DESC")["rows"]

    out = [
        f"📊 <b>Fleet overview</b> · {today()}",
        "",
        f"🚛 Active fleet: <b>{total} trucks</b>",
        f"• 👤 With driver: <b>{with_d}</b>",
        f"• 🅿️ Without driver: <b>{total - with_d}</b> (yard, shop, etc.)",
        f"🔑 Rental trucks: <b>{rental}</b> — counted separately",
        "",
        "🛠 <b>PM</b>",
        f"• Overdue: <b>{pm_over or 0}</b>" + (" ❗️" if pm_over else ""),
        f"• Due within {num(PM_SOON)} mi: <b>{pm_soon or 0}</b>" + (" ⚠️" if pm_soon else ""),
        "",
        "📄 <b>Cab card / inspection</b>",
        f"• Expired: <b>{cab_x or 0}</b> / <b>{insp_x or 0}</b>" + (" ❗️" if (cab_x or insp_x) else ""),
        f"• Expiring in 30 days: <b>{cab_s or 0}</b> / <b>{insp_s or 0}</b>" + (" ⚠️" if (cab_s or insp_s) else ""),
        "",
        "🏢 <b>By group</b>",
    ]
    top, rest = groups[:6], groups[6:]
    out += [f"• {esc(g)} — {c}" for g, c in top]
    if rest:
        out.append(f"• {len(rest)} other groups — {sum(c for _, c in rest)}")
    out += ["", "🏭 " + " · ".join(f"{esc(m)} {c}" for m, c in makes)]
    return Reply("\n".join(out))


def _safe(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum() or ch in " -")[:40]
