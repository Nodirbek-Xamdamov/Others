"""Work order oqimining Telegram qismi: fayl + invoys caption'i -> Fleetio work order.

- Ruxsat: faqat WO_CHAT_IDS (.env) dagi chatlar (guruh ID yoki shaxsiy chat uchun Telegram user ID).
- Bitta fayl (hujjat yoki rasm) caption bilan; albom bo'lsa hamma fayllari biriktiriladi; matn faylga "reply" qilib
  yozilgan bo'lsa ham qabul qilinadi.
- Taxmin qilinmaydi: service task aniqlanmasa yoki shu invoys raqamli work order allaqachon bo'lsa, tugmalar bilan
  so'raladi. Majburiy maydon (Unit, Date) bo'lmasa nima to'g'rilanishi yoziladi; xabar tahrirlansa qayta uriniladi.
  Ixtiyoriy maydonlar bo'lmasa work order ular bo'sh holda yaratiladi va javobda ℹ️ bilan aytiladi.
- Tezlik: bir nechta invoys bir vaqtda (parallel) ishlanadi — navbat faqat bitta xabar ichida; fayl Telegram'dan
  olinib Fleetio'ga yuklanishi reja (Fleetio tekshiruvlari + LLM) bilan bir vaqtda boshlanadi; Fleetio
  ma'lumotnomalari fonda yangilanib turadi (REFRESH_EVERY_S).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import mimetypes
import re
import secrets
import time
from dataclasses import dataclass

from aiogram import Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import fmt
from . import wo_match as M
from .wo_parser import Invoice, looks_like_invoice, parse_invoice
from .workorders import (REFRESH_EVERY_S, FileBlob, Plan, WorkOrders, _drop, format_preview, format_problems,
                         format_result, web_link)

log = logging.getLogger("wo_bot")

ALBUM_WAIT_S = 2.0                      # albom bo'laklari alohida xabar bo'lib keladi — hammasini kutamiz
MAX_FILE_BYTES = 20 * 1024 * 1024       # Telegram bot fayl yuklab olish chegarasi
MAX_FILES = 10
PENDING_TTL_S = 24 * 3600
DENIED_NOTICE_EVERY_S = 3600

NO_FILE = ("📎 The invoice file is missing. Send this text as the <b>caption of the invoice file</b> (photo or PDF), "
           "or reply with it to the message that has the file.")


def _has_file(m: Message) -> bool:
    return bool(m.document or m.photo)


def _file_size(m: Message) -> int:
    return (m.document.file_size if m.document else m.photo[-1].file_size) or 0


@dataclass
class Prepared:
    """Oldindan tayyorlangan fayllar: Telegram'dan olingan baytlar va (bo'lsa) Fleetio'ga yuklangani."""
    blobs: list[FileBlob]
    uploaded: list[dict] | None = None


@dataclass
class Pending:
    """Tugma kutayotgan yoki qayta urinilishi mumkin bo'lgan so'rov."""
    plan: Plan
    key: str                 # jurnal kaliti: chat:message_id
    file_msgs: list[Message]
    status: Message          # botning javob xabari (tahrirlab boriladi)
    at: float
    force_duplicate: bool = False
    prep: asyncio.Task | None = None     # -> Prepared (reja bilan bir vaqtda boshlangan)


class WOBot:
    def __init__(self, svc: WorkOrders):
        self.svc = svc
        self.pending: dict[str, Pending] = {}
        self.albums: dict[str, list[Message]] = {}
        self.recent_albums: dict[str, list[Message]] = {}
        # navbat faqat bitta xabar ichida (asl xabar, uning tahriri va tugmalari); boshqa invoyslar parallel ishlanadi.
        # Bitta vendor/label'ni ikki marta yaratmaslik workorders.RefData.create_lock'da.
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._denied_at: dict[int, float] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ filtrlar
    def _allowed(self, chat_id: int) -> bool:
        return chat_id in self.svc.allowed_chats()

    def is_candidate(self, m: Message) -> bool:
        """Work order oqimiga tegishli xabar: invoys caption'li fayl, ruxsatli chatdagi albom bo'lagi yoki invoys matni."""
        if _has_file(m):
            if looks_like_invoice(m.caption):
                return True
            return m.media_group_id is not None and self._allowed(m.chat.id)
        return looks_like_invoice(m.text)

    def is_wo_event(self, event) -> bool:
        """Login (auth) bu xabarlarga qo'llanmaydi: guruhda login qilib bo'lmaydi, ruxsat WO_CHAT_IDS bilan."""
        if isinstance(event, CallbackQuery):
            return bool((event.data or "").startswith("wo:") and event.message and self._allowed(event.message.chat.id))
        return isinstance(event, Message) and self.is_candidate(event) and self._allowed(event.chat.id)

    def register(self, dp: Dispatcher) -> None:
        dp.message.register(self.on_message, self.is_candidate)
        dp.edited_message.register(self.on_edit, self.is_candidate)
        dp.callback_query.register(self.on_callback, F.data.startswith("wo:"))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def warm_up(self) -> None:
        """Fleetio ma'lumotnomalarini fonda yuklaydi va keyin muntazam yangilaydi (ishlab turgan event loop ichidan)."""
        self._spawn(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        await asyncio.to_thread(self.svc.ref.warm)
        while True:
            await asyncio.sleep(REFRESH_EVERY_S)
            try:
                await asyncio.to_thread(self.svc.ref.refresh)
            except Exception:  # noqa: BLE001 — sikl to'xtamasin
                log.exception("fon yangilash")

    # ------------------------------------------------------------------ kiruvchi xabarlar
    async def on_message(self, msg: Message) -> None:
        if _has_file(msg) and msg.media_group_id:
            self._album_part(msg)
        elif _has_file(msg):
            await self.handle(msg, [msg], msg.caption or "")
        elif msg.reply_to_message is not None and _has_file(msg.reply_to_message):
            await self.handle(msg, [msg.reply_to_message], msg.text or "")
        elif not self._allowed(msg.chat.id):
            await self._deny_notice(msg)
        else:
            await msg.reply(NO_FILE)

    async def on_edit(self, msg: Message) -> None:
        text = (msg.caption if _has_file(msg) else msg.text) or ""
        if not looks_like_invoice(text):
            return
        key = f"{msg.chat.id}:{msg.message_id}"
        if (self.svc.ledger.get(key) or {}).get("status") == "created":
            return  # work order allaqachon yaratilgan — tahrir endi Fleetio'da qilinadi
        if _has_file(msg):
            files = self.recent_albums.get(msg.media_group_id or "") or [msg]
        elif msg.reply_to_message is not None and _has_file(msg.reply_to_message):
            files = [msg.reply_to_message]
        else:
            if self._allowed(msg.chat.id):
                await msg.reply(NO_FILE)
            return
        await self.handle(msg, files, text, edited=True)

    def _album_part(self, msg: Message) -> None:
        gid = str(msg.media_group_id)
        first = gid not in self.albums
        self.albums.setdefault(gid, []).append(msg)
        if first:
            self._spawn(self._album_flush(gid))

    async def _album_flush(self, gid: str) -> None:
        try:
            await asyncio.sleep(ALBUM_WAIT_S)
            msgs = sorted(self.albums.pop(gid, []), key=lambda m: m.message_id)
            lead = next((m for m in msgs if looks_like_invoice(m.caption)), None)
            if lead is None:
                return  # invoys emas — oddiy albom
            self.recent_albums[gid] = msgs[:MAX_FILES]
            while len(self.recent_albums) > 50:
                self.recent_albums.pop(next(iter(self.recent_albums)))
            await self.handle(lead, msgs[:MAX_FILES], lead.caption or "")
        except Exception:  # noqa: BLE001
            log.exception("albom")

    async def _deny_notice(self, msg: Message) -> None:
        now, cid = time.monotonic(), msg.chat.id
        if now - self._denied_at.get(cid, -1e9) < DENIED_NOTICE_EVERY_S:
            return
        self._denied_at[cid] = now
        log.warning("Work order ruxsat yo'q chatdan: %s (%s)", cid, msg.chat.type)
        await msg.reply("⛔ Work orders are not enabled for this chat.\n"
                        f"Chat ID: <code>{cid}</code> — add it to <code>WO_CHAT_IDS</code> in the bot's .env file.")

    # ------------------------------------------------------------------ asosiy oqim
    async def handle(self, msg: Message, file_msgs: list[Message], text: str, edited: bool = False) -> None:
        if not self._allowed(msg.chat.id):
            await self._deny_notice(msg)
            return
        key = f"{msg.chat.id}:{msg.message_id}"
        async with self._lock(key):  # shu xabar bo'yicha ish (tugma bosilgani ham) tugashini kutadi
            job = self.svc.ledger.get(key) or {}
            if job.get("status") == "created":
                wo = (job.get("steps") or {}).get("work_order") or {}
                if not edited:
                    await msg.reply(f"ℹ️ This message was already saved as work order {fmt.esc(str(wo.get('number') or ''))}.")
                return
            same = [t for t, p in self.pending.items() if p.key == key]
            if same and not edited:
                return  # shu xabar bo'yicha tugma kutilmoqda
            for token in same:  # tahrir: eski so'rov (tugmalari) bekor, yangi matn bo'yicha qaytadan
                self._forget(token)
            status = await msg.reply("⏳ Reading the invoice…")
            try:
                await self._process(msg, file_msgs, text, key, status)
            except Exception:  # noqa: BLE001
                log.exception("work order (%s)", key)
                await self._edit(status, "⚠️ Unexpected error while preparing the work order. Nothing was created — "
                                         "please send the message again.")

    async def _process(self, msg: Message, file_msgs: list[Message], text: str, key: str, status: Message) -> None:
        t0 = time.monotonic()
        try:
            today = await asyncio.to_thread(self.svc.ref.today)
        except Exception:  # noqa: BLE001 — Fleetio javob bermasa server sanasi
            today = dt.date.today()
        inv = parse_invoice(text, today=today)
        too_big = any(_file_size(m) > MAX_FILE_BYTES for m in file_msgs)
        # fayl Telegram'dan olinib Fleetio'ga yuklanishi reja bilan bir vaqtda boshlanadi (bir necha soniya tejaladi);
        # reja rad etilsa yuklangan fayl hech narsaga biriktirilmaydi
        prep = self._spawn(self._prepare(file_msgs, inv)) if inv.ok and not too_big and not self.svc.dry_run() else None
        try:
            plan = await self.svc.plan(inv)
        except BaseException:
            _drop(prep)
            raise
        if too_big:
            plan.problems.append("The file is larger than 20 MB — Telegram bots cannot download it. Send a smaller copy.")
        who = msg.from_user.full_name if msg.from_user else ""
        if plan.problems:
            _drop(prep)
            job = self.svc.ledger.get(key) or {}
            job.update(status="rejected", by=who, problems=[fmt.strip_html(x) for x in plan.problems])
            self.svc.ledger.put(key, job)
            log.info("Work order rad etildi (%s): %s", key, " | ".join(job["problems"]))
            await self._edit(status, format_problems(plan))
            return
        log.info("Work order rejasi tayyor (%s): %.1f s", key, time.monotonic() - t0)
        job = self.svc.ledger.get(key) or {}
        job.update(status="pending", by=who)
        self.svc.ledger.put(key, job)
        self._prune()
        token = secrets.token_hex(4)
        self.pending[token] = Pending(plan, key, file_msgs, status, time.monotonic(), prep=prep)
        await self._advance(token)

    def _forget(self, token: str) -> None:
        p = self.pending.pop(token, None)
        if p is not None:
            _drop(p.prep)

    def _lock(self, key: str) -> asyncio.Lock:
        if len(self._key_locks) > 2000:  # eski (bo'sh, kutuvchisiz) qulflar tozalanadi
            for k, lk in list(self._key_locks.items()):
                if not lk.locked() and not getattr(lk, "_waiters", None):
                    self._key_locks.pop(k, None)
        return self._key_locks.setdefault(key, asyncio.Lock())

    def _prune(self) -> None:
        now = time.monotonic()
        for token, p in list(self.pending.items()):
            if now - p.at > PENDING_TTL_S:
                self._forget(token)

    async def _advance(self, token: str) -> None:
        """Keyingi qadam: takror so'rovi -> service task so'rovi -> (sinov rejimi) -> Fleetio'da yaratish."""
        p = self.pending[token]
        plan, e = p.plan, fmt.esc
        unit = e(str(plan.vehicle["name"]))
        if plan.duplicate and not p.force_duplicate:
            dup = plan.duplicate
            await self._edit(p.status,
                             f"⚠️ Work order {e(str(dup.get('number') or ''))} for unit <b>{unit}</b> already has invoice number "
                             f"<b>{e(plan.inv.ref)}</b>.\n"
                             f'<a href="{web_link("work_orders", dup["id"])}">Open it in Fleetio</a>\n\nCreate another one anyway?',
                             [[InlineKeyboardButton(text="➕ Create anyway", callback_data=f"wo:d:{token}"),
                               InlineKeyboardButton(text="✖ Cancel", callback_data=f"wo:x:{token}")]])
            return
        if plan.task is None and not plan.task_skipped:
            if not plan.task_options:
                self.svc.skip_task(plan, f"No service task found for “{e(plan.inv.purpose)}” — the work order has no line item.")
                await self._advance(token)
                return
            rows = [[InlineKeyboardButton(text=t["name"][:60], callback_data=f"wo:t:{token}:{t['id']}")]
                    for t in plan.task_options]
            rows.append([InlineKeyboardButton(text="➖ No service task", callback_data=f"wo:n:{token}"),
                         InlineKeyboardButton(text="✖ Cancel", callback_data=f"wo:x:{token}")])
            await self._edit(p.status, f"❓ Unit <b>{unit}</b> · {e(plan.inv.purpose)}\nWhich <b>service task</b> is this? "
                                       "I will remember your choice for next time.", rows)
            return
        if self.svc.dry_run():
            self._forget(token)
            await self._edit(p.status, format_preview(plan, len(p.file_msgs)))
            return
        await self._execute(token)

    async def _execute(self, token: str) -> None:
        p = self.pending[token]
        retry = [[InlineKeyboardButton(text="🔁 Retry", callback_data=f"wo:r:{token}"),
                  InlineKeyboardButton(text="✖ Cancel", callback_data=f"wo:x:{token}")]]
        await self._edit(p.status, "⏳ Creating the work order in Fleetio…")
        t0 = time.monotonic()
        ready = await self._prepared(p)
        if ready is None:
            try:
                ready = Prepared(await self._fetch(p.file_msgs, p.plan.inv, str(p.plan.vehicle["name"])))
            except Exception as ex:  # noqa: BLE001
                log.warning("Telegram'dan fayl yuklab bo'lmadi (%s): %s", p.key, ex)
                await self._edit(p.status, "⚠️ Could not download the file from Telegram. Nothing was created yet.", retry)
                return
        job = await self.svc.execute(p.plan, ready.blobs, p.key, ready.uploaded)
        if job.get("status") == "created":
            self.pending.pop(token, None)
            wo = job["steps"]["work_order"]
            log.info("Work order yaratildi: %s -> %s (id %s), issue %s, %.1f s", p.key, wo.get("number"), wo.get("id"),
                     (job["steps"].get("issue") or {}).get("number"), time.monotonic() - t0)
            await self._edit(p.status, format_result(p.plan, job))
        else:
            await self._edit(p.status, format_result(p.plan, job), retry)

    async def _prepared(self, p: Pending) -> Prepared | None:
        """Oldindan boshlangan tayyorlov natijasi (kerak bo'lsa tugashini kutadi); bo'lmasa yoki xato bo'lsa None."""
        if p.prep is None:
            return None
        try:
            return await asyncio.shield(p.prep)
        except Exception as ex:  # noqa: BLE001 — oddiy yo'l bilan qayta yuklanadi
            log.warning("Oldindan tayyorlangan fayl ishlatilmadi (%s): %s", p.key, ex)
            p.prep = None
            return None

    async def _prepare(self, file_msgs: list[Message], inv: Invoice) -> Prepared:
        """Telegram'dan olish + Fleetio'ga yuklash (albom fayllari parallel). Yuklash bo'lmasa — keyin qayta uriniladi."""
        blobs = await self._fetch(file_msgs, inv, M.pick_target(inv)[0] or inv.unit)
        try:
            uploaded = await asyncio.gather(*(asyncio.to_thread(self.svc.writer.upload, b.data, b.name, b.mime)
                                              for b in blobs))
        except Exception as ex:  # noqa: BLE001
            log.warning("Faylni oldindan Fleetio'ga yuklab bo'lmadi: %s", ex)
            return Prepared(blobs)
        return Prepared(blobs, list(uploaded))

    @staticmethod
    async def _fetch(file_msgs: list[Message], inv: Invoice, unit: str) -> list[FileBlob]:
        day = f"_{inv.date:%Y-%m-%d}" if inv.date else ""
        base = re.sub(r"[^\w.-]+", "_", f"Unit_{unit}{day}" + (f"_Ref_{inv.ref}" if inv.ref else ""))

        async def one(i: int, m: Message) -> FileBlob:
            if m.document:
                file_id = m.document.file_id
                name = m.document.file_name or f"{base}_{i}"
                mime = m.document.mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
            else:
                file_id, mime = m.photo[-1].file_id, "image/jpeg"
                name = f"{base}.jpg" if len(file_msgs) == 1 else f"{base}_{i}.jpg"
            buf = await m.bot.download(file_id, timeout=120)
            return FileBlob(name, mime, buf.read())

        return list(await asyncio.gather(*(one(i, m) for i, m in enumerate(file_msgs, 1))))

    # ------------------------------------------------------------------ tugmalar
    async def on_callback(self, cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        action, token = (parts + ["", ""])[1:3]
        if cb.message is None or not self._allowed(cb.message.chat.id):
            await cb.answer("Work orders are not enabled for this chat.", show_alert=True)
            return
        p = self.pending.get(token)
        if p is None:
            await cb.answer("This request has expired — send the message again.", show_alert=True)
            return
        await cb.answer()
        async with self._lock(p.key):  # ikki marta bosilsa yoki shu payt xabar tahrirlansa — navbat bilan
            if self.pending.get(token) is not p:
                return  # oldingi bosish (yoki tahrir) allaqachon bajargan
            try:
                if action == "x":
                    self._forget(token)
                    job = self.svc.ledger.get(p.key) or {}
                    job["status"] = "cancelled"
                    self.svc.ledger.put(p.key, job)
                    await self._edit(p.status, "✖ Cancelled.")
                    return
                if action == "t":
                    task = self.svc.ref.book().task(parts[3] if len(parts) > 3 else None)
                    if task is None:
                        return
                    p.plan.task, p.plan.task_how = task, "chosen"
                    await asyncio.to_thread(self.svc.ref.learn_task, p.plan.inv.purpose, task["id"])
                elif action == "n":
                    self.svc.skip_task(p.plan, "No service task chosen — the work order has no line item.")
                elif action == "d":
                    p.force_duplicate = True
                elif action != "r":
                    return
                await self._advance(token)
            except Exception:  # noqa: BLE001
                log.exception("work order tugmasi (%s)", p.key)
                await self._edit(p.status, "⚠️ Unexpected error. Check Fleetio before sending the message again.")

    async def _edit(self, status: Message, text: str, rows: list[list[InlineKeyboardButton]] | None = None) -> None:
        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        try:
            await status.edit_text(text[:4000], reply_markup=markup)
        except TelegramBadRequest as ex:
            if "not modified" in str(ex):
                return
            log.warning("HTML rad etildi (%s), oddiy matn yuborilmoqda", str(ex)[:120])
            await status.edit_text(fmt.strip_html(text)[:4000], parse_mode=None, reply_markup=markup)
