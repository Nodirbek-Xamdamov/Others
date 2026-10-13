"""Work order oqimining Telegram qismi: fayl + invoys caption'i -> Fleetio work order.

- Ruxsat: faqat WO_CHAT_IDS (.env) dagi chatlar (guruh ID yoki shaxsiy chat uchun Telegram user ID).
- Bitta fayl (hujjat yoki rasm) caption bilan; albom bo'lsa hamma fayllari biriktiriladi; matn faylga "reply" qilib
  yozilgan bo'lsa ham qabul qilinadi.
- Taxmin qilinmaydi: service task aniqlanmasa yoki shu invoys raqamli work order allaqachon bo'lsa, tugmalar bilan
  so'raladi. Xabarda xato bo'lsa nima to'g'rilanishi yoziladi; xabar tahrirlansa qayta uriniladi.
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
from .wo_parser import looks_like_invoice, parse_invoice
from .workorders import FileBlob, Plan, WorkOrders, format_preview, format_problems, format_result, web_link

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


@dataclass
class Pending:
    """Tugma kutayotgan yoki qayta urinilishi mumkin bo'lgan so'rov."""
    plan: Plan
    key: str                 # jurnal kaliti: chat:message_id
    file_msgs: list[Message]
    status: Message          # botning javob xabari (tahrirlab boriladi)
    at: float
    force_duplicate: bool = False


class WOBot:
    def __init__(self, svc: WorkOrders):
        self.svc = svc
        self.pending: dict[str, Pending] = {}
        self.albums: dict[str, list[Message]] = {}
        self.recent_albums: dict[str, list[Message]] = {}
        self.lock = asyncio.Lock()   # bir vaqtda bitta work order: ikki xabar bitta vendorni ikki marta yaratmasin
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

    def warm_up(self) -> None:
        """Fleetio ma'lumotnomalarini fon rejimida yuklaydi (ishlab turgan event loop ichidan chaqiriladi)."""
        task = asyncio.create_task(asyncio.to_thread(self.svc.ref.warm))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
        for token, p in list(self.pending.items()):
            if p.key == key:
                self.pending.pop(token, None)
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
            task = asyncio.create_task(self._album_flush(gid))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

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
        job = self.svc.ledger.get(key) or {}
        if job.get("status") == "created":
            wo = (job.get("steps") or {}).get("work_order") or {}
            if not edited:
                await msg.reply(f"ℹ️ This message was already saved as work order {fmt.esc(str(wo.get('number') or ''))}.")
            return
        if not edited and any(p.key == key for p in self.pending.values()):
            return  # shu xabar bo'yicha tugma kutilmoqda
        status = await msg.reply("⏳ Reading the invoice…")
        async with self.lock:
            try:
                await self._process(msg, file_msgs, text, key, status)
            except Exception:  # noqa: BLE001
                log.exception("work order (%s)", key)
                await self._edit(status, "⚠️ Unexpected error while preparing the work order. Nothing was created — "
                                         "please send the message again.")

    async def _process(self, msg: Message, file_msgs: list[Message], text: str, key: str, status: Message) -> None:
        try:
            today = await asyncio.to_thread(self.svc.ref.today)
        except Exception:  # noqa: BLE001 — Fleetio javob bermasa server sanasi
            today = dt.date.today()
        inv = parse_invoice(text, today=today)
        plan = await self.svc.plan(inv)
        for m in file_msgs:
            size = (m.document.file_size if m.document else m.photo[-1].file_size) or 0
            if size > MAX_FILE_BYTES:
                plan.problems.append("The file is larger than 20 MB — Telegram bots cannot download it. Send a smaller copy.")
                break
        who = msg.from_user.full_name if msg.from_user else ""
        if plan.problems:
            job = self.svc.ledger.get(key) or {}
            job.update(status="rejected", by=who, problems=[fmt.strip_html(x) for x in plan.problems])
            self.svc.ledger.put(key, job)
            log.info("Work order rad etildi (%s): %s", key, " | ".join(job["problems"]))
            await self._edit(status, format_problems(plan))
            return
        job = self.svc.ledger.get(key) or {}
        job.update(status="pending", by=who)
        self.svc.ledger.put(key, job)
        self._prune()
        token = secrets.token_hex(4)
        self.pending[token] = Pending(plan, key, file_msgs, status, time.monotonic())
        await self._advance(token)

    def _prune(self) -> None:
        now = time.monotonic()
        for token, p in list(self.pending.items()):
            if now - p.at > PENDING_TTL_S:
                self.pending.pop(token, None)

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
        if plan.task is None:
            if not plan.task_options:
                self.pending.pop(token, None)
                await self._edit(p.status, f"⚠️ <b>Work order not created</b>\n• No service task found for “{e(plan.inv.purpose)}”. "
                                           "Reword the <code>For:</code> line (edit the message) or send it again.")
                return
            rows = [[InlineKeyboardButton(text=t["name"][:60], callback_data=f"wo:t:{token}:{t['id']}")]
                    for t in plan.task_options]
            rows.append([InlineKeyboardButton(text="✖ Cancel", callback_data=f"wo:x:{token}")])
            await self._edit(p.status, f"❓ Unit <b>{unit}</b> · {e(plan.inv.purpose)}\nWhich <b>service task</b> is this? "
                                       "I will remember your choice for next time.", rows)
            return
        if self.svc.dry_run():
            self.pending.pop(token, None)
            await self._edit(p.status, format_preview(plan, len(p.file_msgs)))
            return
        await self._execute(token)

    async def _execute(self, token: str) -> None:
        p = self.pending[token]
        retry = [[InlineKeyboardButton(text="🔁 Retry", callback_data=f"wo:r:{token}"),
                  InlineKeyboardButton(text="✖ Cancel", callback_data=f"wo:x:{token}")]]
        await self._edit(p.status, "⏳ Creating the work order in Fleetio…")
        try:
            files = await self._download(p)
        except Exception as ex:  # noqa: BLE001
            log.warning("Telegram'dan fayl yuklab bo'lmadi (%s): %s", p.key, ex)
            await self._edit(p.status, "⚠️ Could not download the file from Telegram. Nothing was created yet.", retry)
            return
        job = await self.svc.execute(p.plan, files, p.key)
        if job.get("status") == "created":
            self.pending.pop(token, None)
            wo = job["steps"]["work_order"]
            log.info("Work order yaratildi: %s -> %s (id %s), issue %s", p.key, wo.get("number"), wo.get("id"),
                     job["steps"]["issue"].get("number"))
            await self._edit(p.status, format_result(p.plan, job))
        else:
            await self._edit(p.status, format_result(p.plan, job), retry)

    async def _download(self, p: Pending) -> list[FileBlob]:
        inv = p.plan.inv
        base = re.sub(r"[^\w.-]+", "_", f"Unit_{p.plan.vehicle['name']}_{inv.date:%Y-%m-%d}" + (f"_Ref_{inv.ref}" if inv.ref else ""))
        out = []
        for i, m in enumerate(p.file_msgs, 1):
            if m.document:
                file_id = m.document.file_id
                name = m.document.file_name or f"{base}_{i}"
                mime = m.document.mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
            else:
                file_id, mime = m.photo[-1].file_id, "image/jpeg"
                name = f"{base}.jpg" if len(p.file_msgs) == 1 else f"{base}_{i}.jpg"
            buf = await m.bot.download(file_id, timeout=120)
            out.append(FileBlob(name, mime, buf.read()))
        return out

    # ------------------------------------------------------------------ tugmalar
    async def on_callback(self, cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        action, token = (parts + ["", ""])[1:3]
        if cb.message is None or not self._allowed(cb.message.chat.id):
            await cb.answer("Work orders are not enabled for this chat.", show_alert=True)
            return
        if token not in self.pending:
            await cb.answer("This request has expired — send the message again.", show_alert=True)
            return
        await cb.answer()
        async with self.lock:
            p = self.pending.get(token)
            if p is None:
                return  # oldingi bosish allaqachon bajargan
            try:
                if action == "x":
                    self.pending.pop(token, None)
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
