"""Vaqtinchalik login/parol: /start dan keyin bot login va parol so'raydi.

- Login/parol `.env` da (BOT_LOGIN, BOT_PASSWORD); ikkalasi bo'sh bo'lsa auth o'chiq.
- Kirgan foydalanuvchilar `data/authorized_users.json` da saqlanadi (restartdan keyin ham kirgan bo'lib qoladi).
  Login/parol o'zgarsa, eski sessiyalar bekor bo'ladi (fayldagi barmoq izi mos kelmaydi).
- Parol yozilgan xabar chatdan o'chiriladi; 5 marta xato -> 15 daqiqa blok; login faqat shaxsiy chatda.
- aiogram outer middleware: login bo'lmagan foydalanuvchining xabari handlerlarga (va LLM'ga) yetib bormaydi.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

log = logging.getLogger("auth")

MAX_ATTEMPTS = 5
LOCK_S = 15 * 60


class Auth:
    def __init__(self, login: str, password: str, store: Path):
        self._login, self._password = login or "", password or ""
        self.store = Path(store)
        self._fp = hashlib.sha256(f"{self._login}\0{self._password}".encode()).hexdigest()[:16]
        self.users: dict[str, dict] = self._load()
        self.pending: dict[int, dict] = {}                 # uid -> {"step": "login"|"password", "login": ...}
        self.fails: dict[int, tuple[int, float]] = {}     # uid -> (xatolar soni, blok tugash vaqti)

    @property
    def enabled(self) -> bool:
        return bool(self._login and self._password)

    def _load(self) -> dict[str, dict]:
        try:
            d = json.loads(self.store.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        if d.get("fingerprint") != self._fp:  # login/parol o'zgargan -> hamma qayta kiradi
            return {}
        return d.get("users", {})

    def _save(self) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fingerprint": self._fp, "users": self.users}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.store)

    def is_authorized(self, uid: int) -> bool:
        return not self.enabled or str(uid) in self.users

    def check(self, login: str, password: str) -> bool:
        ok_login = hmac.compare_digest(login.strip().encode(), self._login.encode())
        ok_pass = hmac.compare_digest(password.encode(), self._password.encode())
        return ok_login and ok_pass

    def authorize(self, uid: int, name: str) -> None:
        self.users[str(uid)] = {"name": name, "at": dt.datetime.now().isoformat(timespec="seconds")}
        self.pending.pop(uid, None)
        self.fails.pop(uid, None)
        self._save()

    def logout(self, uid: int) -> None:
        self.users.pop(str(uid), None)
        self.pending.pop(uid, None)
        self._save()

    def locked_for(self, uid: int) -> float:
        n, until = self.fails.get(uid, (0, 0.0))
        return max(0.0, until - time.monotonic())

    def fail(self, uid: int) -> None:
        n, _ = self.fails.get(uid, (0, 0.0))
        n += 1
        self.fails[uid] = (0, time.monotonic() + LOCK_S) if n >= MAX_ATTEMPTS else (n, 0.0)


ASK_LOGIN = "🔐 This bot requires a login.\nPlease enter your <b>login</b>:"
ASK_PASSWORD = "Now enter your <b>password</b> (the message will be deleted):"
WRONG = "❌ Wrong login or password.\nPlease enter your <b>login</b>:"
PRIVATE_ONLY = "🔐 Please log in first in a private chat with me."


class AuthMiddleware(BaseMiddleware):
    """Login bo'lmaganlar uchun login oqimini yuritadi; kirganlarni handlerlarga o'tkazadi."""

    def __init__(self, auth: Auth, on_login: Callable[[Message], Awaitable] | None = None,
                 allowed: Callable[[int | None], bool] | None = None,
                 bypass: Callable[[TelegramObject], Awaitable[bool]] | None = None):
        self.auth, self.on_login, self.allowed, self.bypass = auth, on_login, allowed, bypass

    async def __call__(self, handler, event: TelegramObject, data: dict):
        # login so'ralmaydigan hodisalar: work order oqimi (o'z ruxsat ro'yxati bor) va guruhdagi botga qaratilmagan xabarlar
        if self.bypass is not None and await self.bypass(event):
            return await handler(event, data)
        user = getattr(event, "from_user", None)
        uid = user.id if user else None
        # auth o'chiq yoki ALLOWED_USER_IDS ro'yxatida yo'q (bular uchun 'denied' handler bor) -> odatdagidek
        if not self.auth.enabled or uid is None or (self.allowed and not self.allowed(uid)):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            if self.auth.is_authorized(uid):
                return await handler(event, data)
            await event.answer("Please log in first: send /start", show_alert=True)
            return None
        if not isinstance(event, Message):
            return await handler(event, data)
        text = (event.text or "").strip()
        if self.auth.is_authorized(uid):
            if text.split("@")[0] == "/logout":
                self.auth.logout(uid)
                log.info("logout: %s", uid)
                await event.answer("👋 Logged out. Send /start to log in again.")
                return None
            return await handler(event, data)
        await self._login_flow(event, uid, text)
        return None

    async def _login_flow(self, msg: Message, uid: int, text: str) -> None:
        if msg.chat.type != ChatType.PRIVATE:
            await msg.answer(PRIVATE_ONLY)
            return
        wait = self.auth.locked_for(uid)
        if wait:
            await msg.answer(f"⛔️ Too many wrong attempts. Try again in {int(wait // 60) + 1} min.")
            return
        st = self.auth.pending.get(uid)
        if st is None or not text or text.startswith("/"):
            self.auth.pending[uid] = {"step": "login"}
            await msg.answer(ASK_LOGIN)
            return
        if st["step"] == "login":
            self.auth.pending[uid] = {"step": "password", "login": text}
            await msg.answer(ASK_PASSWORD)
            return
        try:  # parol chatda qolmasin
            await msg.delete()
        except Exception:  # noqa: BLE001
            log.warning("parol xabarini o'chirib bo'lmadi (uid=%s)", uid)
        if self.auth.check(st["login"], text):
            name = " ".join(x for x in (msg.from_user.first_name, msg.from_user.last_name) if x) if msg.from_user else ""
            self.auth.authorize(uid, name)
            log.info("login OK: %s (%s)", uid, name)
            await msg.answer("✅ Logged in.")
            if self.on_login:
                await self.on_login(msg)
            return
        self.auth.fail(uid)
        self.auth.pending[uid] = {"step": "login"}
        log.warning("login xato: uid=%s", uid)
        wait = self.auth.locked_for(uid)
        await msg.answer(f"⛔️ Too many wrong attempts. Try again in {LOCK_S // 60} min." if wait else WRONG)
