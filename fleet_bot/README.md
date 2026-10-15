# Fleet Telegram Bot (demo)

<!-- qisqacha -->
> **Qisqacha.** Avtopark bo'yicha Telegram bot: savollarga Fleetio (Secure Fleet API) ma'lumotidan javob beradi (faqat o'qish) va guruhga tashlangan invoys fayli asosida Fleetio'da work order ochadi.
>
> **Texnologiyalar:** Python, Telegram bot, LLM, Fleetio API
>
> **Ishga tushirish:** `python main.py` (sozlamalar `.env` da, namuna: `.env.example`)

Park bo'yicha savollarga javob beradigan Telegram bot. Ikki rejim (`.env` dagi `DATA_SOURCE`):
- **`fleetio`** — javoblar **Fleetio saytidan** (Secure Fleet API), jonli ma'lumot, **faqat o'qish**;
- **`sheet`** — "Fleet Truck Info Board" xlsx jadvalidan (eski rejim, pastda batafsil).

Demo bot: **@david_prototype_bot**

## Fleetio rejimi

1. `.env` ga `SECUREFLEET_KEY` (API key) qo'ying. `SECUREFLEET_ACCOUNT_TOKEN` va `DATA_SOURCE=fleetio` allaqachon bor.
   `.env` o'zgarsa darhol ko'rinadi — key qo'shgach botni qayta ishga tushirish shart emas.
2. **Tezkor hisobotlar** (LLM'siz, `app/fleetio_reports.py`) — pastdagi tugmalar yoki buyruqlar:
   🚚 Fleet overview `/overview` · 🛠 Service due `/service` · 📄 Renewals `/renewals` · ⚠️ Open issues `/issues` ·
   🅿️ No driver `/nodriver` · mashina kartasi — faqat unit raqamini yozing (`417`, `unit 3871`, `0832 ED`) yoki `/unit 417`.
   Ma'lumot Fleetio API'dan to'g'ridan-to'g'ri olinadi, sanashni Python qiladi: javob 0–1 s (LLM orqali 10–60 s edi).
   Og'ir hisobotlar (service ~1300 reminder, renewals) fonda har 10 daqiqada tayyorlanadi; keshdan berilsa ma'lumot yoshi yoziladi.
   Jadval rejimidan qolgan eski tugmalar (📊 Fleet overview, 🛠 PM status, 📄 Expirations) ham ishlaydi — bosilganda yangi klaviatura yuboriladi.
3. Boshqa savollar oddiy so'z bilan: "Who is driving unit 417?", "Open issues for 417", "Repair costs of 417 this year" — LLM orqali.
   Kutish paytida bot nima tekshirilayotganini yozib turadi ("🔎 Checking Fleetio: fuel entries…").
4. `/api` — oxirgi javob uchun Fleetio'ga yuborilgan so'rovlar (tekshirish uchun). `/reset` — suhbat kontekstini tozalash.
5. **Login** (vaqtinchalik, `app/auth.py`): `.env` da `BOT_LOGIN` / `BOT_PASSWORD` bo'lsa, `/start` dan keyin bot login va parol
   so'raydi (faqat shaxsiy chatda). Parol xabari chatdan o'chiriladi, 5 xato -> 15 daqiqa blok, `/logout` — chiqish.
   Kirganlar `data/authorized_users.json` da; login/parol o'zgartirilsa hamma qayta kiradi. Ikkala rejimda ishlaydi.

```
Telegram ─> tugma / unit raqami ─> fleetio_reports (Python) ─────────────────────────────> Fleetio API (GET) ─> Telegram
        └─> erkin savol ─> LLM ─fleetio_get / fleetio_help─> Fleetio API (faqat GET) ─> LLM javob yozadi ─> Telegram
```

- **Tool'lar:** `data/fleetio_tools_readonly.json` — rasmiy OpenAPI hujjatidan yaratilgan 133 ta GET (to'liq o'qish huquqi).
  LLM'ga 133 ta tool ta'rifi o'rniga 2 ta meta-tool beriladi (token tejash): `fleetio_get` (katalogdagi istalgan tool'ni
  bajaradi) va `fleetio_help` (parametrlarini ko'rsatadi); nomlar katalogi system prompt'da.
- **Xavfsizlik** (`app/fleetio.py`): faqat GET, faqat katalogdagi yo'llar, noma'lum parametrlar tashlanadi; kalitlar faqat `.env` dan,
  log va `/api` chiqishida kalit yo'q. Yozish/o'chirish tool'lari botga berilmagan.
- **Aniqlik:** "nechta / status bo'yicha" savollarida sanashni Python qiladi (`count_by`, 1000 tagacha yozuv), LLM faqat natijani yozadi.
- **Tezlik:** Fleetio'ga so'rovlar bitta keep-alive ulanishlar havzasi orqali ketadi (`app/net.py`, httpx2) — har so'rovga yangi
  TCP+TLS ulanish ochilmaydi (~0.3 s tejaladi). Bir raunddagi tool-chaqiruvlar parallel. Ro'yxat yozuvlariga mashina nomi
  qo'shiladi (`vehicle_id` -> `vehicle_name`), shuning uchun model har mashina uchun alohida `get_vehicle` chaqirmaydi.
- **Token tejash:** javob ixchamlanadi (bo'sh maydonlar, rasm havolalari, 0 hisoblagichlar olib tashlanadi, ~5000 belgigacha).
- **Cheklov:** murakkab savol 2–4 ta LLM chaqiruv. Limitga (429) tushsa bot kutmasdan `LLM_FALLBACK_MODEL` ga o'tadi.
- Testlar: `python -m tests.test_offline`, `python -m tests.test_fast` (keep-alive, hisobotlar). Terminalda: `python ask.py "Who drives 417?"`.

## Work order (invoys fayli -> Fleetio)

Ruxsatli chatga (`.env`: `WO_CHAT_IDS`) invoys fayli (rasm/PDF) quyidagi caption bilan tashlanadi — bot Fleetio'da
**Service > Work Orders** ga work order yaratadi va natijani shu xabarga javob qilib yozadi:

```
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
```

| Fleetio maydoni | Qayerdan |
|---|---|
| Vehicle | `Unit` (Trailer qatori bo'lib, ish faqat treylerga tegishli bo'lsa — treyler) |
| Status | doim Completed |
| Repair Priority Class | `For`/`Note` da Roadside, Road side, RS, RC bo'lsa Emergency, aks holda Scheduled |
| Issued by | API key egasi (`GET /users/me`) |
| Issue / Start / Expected / Actual Completion Date | `Date` |
| Assigned To | `Driver` (kontaktlardan; topilmasa bo'sh qoladi) |
| Vendor | oxirgi blok: nom + manzil + telefon bo'yicha qidiriladi, topilmasa yaratiladi (manzili bo'lmasa faqat nomi bilan); ustaxona yozilmasa — bo'sh |
| Invoice Number, PO Number | `Ref` |
| Description | xabar matni (ustaxona blokisiz) |
| Issue (yangi) | Summary = `For`, No Priority, Reported = `Date`, Label = `Responsible`, Reference Number = `Ref`, fayl(lar) Documents'da. `For` bo'lmasa issue yo'q — fayl va label work order'ga |
| Line item | `For` bo'yicha service task, Subtotal = `Amount` (Amount bo'lmasa narxsiz, task topilmasa line item'siz) |

- **Majburiy faqat `Unit` va `Date`** (Fleetio'ning o'zida ham Vehicle va Issue Date majburiy). Qolganlari bo'lmasa work order
  ular bo'sh holda yaratiladi va javobda ℹ️ bilan aytiladi. Unit topilmasa/noaniq bo'lsa, sana yo'q/noto'g'ri bo'lsa — yaratilmaydi.
- **Tezlik:** bir nechta invoys bir vaqtda (parallel) ishlanadi; bitta vendor/label ikki marta yaratilmaydi (`RefData.create_lock`).
  Fayl Telegram'dan olinib Fleetio'ga yuklanishi va service task'ni LLM bilan tanlash reja bilan bir vaqtda boshlanadi.
  Ma'lumotnomalar fonda yangilanadi (mashinalar va vendorlar 5 daqiqada, kontaktlar/labellar 30 daqiqada, vendorlar to'liq — kuniga),
  shuning uchun invoys kelganda Fleetio ro'yxatlari kutilmaydi.
- **Kod:** `app/wo_parser.py` (matnni ajratish), `app/wo_match.py` (Fleetio yozuvlariga moslash), `app/workorders.py`
  (reja + yaratish + jurnal), `app/wo_bot.py` (Telegram), `app/fleetio_write.py` (yozish: faqat vendor/label/issue/work order
  yaratish va fayl yuklash; savol-javob qismi avvalgidek faqat GET).
- **Service task:** `data/fleetio_service_tasks.json` — Fleetio'dagi hamma tasklar; `data/service_task_examples.json` —
  eski service entry'larda jamoa "For" matniga qaysi taskni tanlagani. Aniq bo'lmasa LLM tanlaydi, u ham ishonmasa bot
  tugmalar bilan so'raydi va tanlovni eslab qoladi (`data/service_task_aliases.json`).
- **Takror va uzilish:** shu mashina + shu `Ref` bilan work order bo'lsa bot so'raydi. Har xabar natijasi
  `data/work_orders_log.json` da; o'rtada xato chiqsa "Retry" yaratilgan narsani qayta yaratmaydi.
- **Sinov:** `WO_DRY_RUN=1` — Fleetio'ga yozmasdan nima yaratilishini ko'rsatadi. Terminalda:
  `python -m app.workorders check xabar.txt`. Ro'yxatlarni yangilash: `python -m app.workorders tasks` / `examples`.
- **Guruh:** bot hamma xabarni ko'rishi uchun BotFather'da `/setprivacy -> Disable` (keyin botni guruhga qayta qo'shish)
  yoki botni guruhda admin qilish kerak. Guruhda bot faqat invoys xabarlariga va o'ziga yozilgan (@mention / reply) savollarga javob beradi.
- Testlar: `python -m tests.test_workorders` (parallel invoyslar va fon keshi ham).

---

Quyidagi bo'limlar **jadval rejimi** (`DATA_SOURCE=sheet`) haqida.

## Ishga tushirish (macOS)

1. Terminal oching va loyiha papkasiga o'ting:
   ```bash
   cd ~/Desktop/fleet_bot
   ./run.sh
   ```
   Yoki Finder'da **`Botni ishga tushirish.command`** faylini ikki marta bosing
   (birinchi marta macOS ogohlantirsa: o'ng tugma → Open).
2. Birinchi ishga tushishda skript Telegram token va Groq API key'ni so'raydi va `.env` faylini yaratadi,
   so'ng `.venv` yaratib kutubxonalarni o'rnatadi (1-2 daqiqa). Keyingi safarlar hech narsa so'ramaydi.
3. Terminalda `Bot ishga tushdi: @david_prototype_bot` yozuvi chiqsa, Telegram'da botga `/start` yozing.

To'xtatish: terminalda `Ctrl+C`. Bot kompyuter yoqiq va terminal ochiq turgan paytda ishlaydi.

Telegram'siz sinash (terminalda savol-javob):
```bash
source .venv/bin/activate
python ask.py "PM muddati o'tgan mashinalar qaysilar?"
python ask.py            # interaktiv rejim
```

Internetsiz testlar (jadval, SQL xavfsizligi, handlerlar): `python -m tests.test_offline`

## Nimalarni so'rash mumkin

Savolni o'zbek, rus yoki ingliz tilida yozish mumkin — **bot har doim ingliz tilida javob beradi**.

- "417", "417 unit kimda?", "John Smith qaysi mashinada?", "0397" (VIN/plate oxiri)
- "PM muddati o'tib ketgan mashinalar", "PM gacha 2000 mildan kam qolganlar"
- "Keyingi 60 kunda inspeksiyasi tugaydiganlar", "Cab card muddati o'tganlar"
- "Riverview guruhida nechta mashina bor?", "Guruhlar bo'yicha haydovchisiz mashinalar soni"
- "Haydovchisi yo'q mashinalar qayerda turibdi?", "Eng ko'p probegli 5 ta mashina", "2026 yilgi Volvo'lar"
- "6254 da oxirgi qanday muammo bo'lgan?" (Sheet103), "Hozir yo'lda bo'lgan mashinalar" (Samsara GPS varag'i)
- "Ijaradagi mashinalar nechta va nechtasi shopda?" (Rental trucks)

Pastdagi menyu tugmalari (AI'siz, darhol): **📊 Fleet overview · 🛠 PM status · 📄 Expirations · 🅿️ No driver · ❓ Help**.
Ro'yxat tagidagi **🚛 unit** tugmasi bosilsa — o'sha mashinaning to'liq kartasi chiqadi.

Buyruqlar: `/stats` · `/pm` · `/expiring` (yoki `/expiring 60`) · `/nodriver` · `/unit 417` · `/reset` · `/refresh` · `/sql`

## Qanday ishlaydi

```
Telegram ─> router (kalit so'zlar, AI'siz) ──> tayyor hisobot/karta ─────────────┐
              │ tanimasa                                                        ▼
              └─> LLM (Groq): SQL + sarlavha ─run_sql─> SQLite ─> Python formatlaydi ─> Telegram (HTML)
```

- **Router** (`app/router.py`): unit raqami, haydovchi ismi, "nechta mashina", PM, cab card/inspeksiya,
  haydovchisizlar — AI'siz, ~10 ms. Savolda notanish so'z bo'lsa (guruh, marka, yil...) LLM'ga o'tadi.
- **LLM faqat SQL yozadi** (bitta chaqiruv). Natijani LLM qayta yozmaydi — `app/agent.py:render` uni sarlavha +
  ro'yxat qilib formatlaydi. Shuning uchun raqamlar aynan SQLite hisoblagan qiymat (LLM qo'shib-ayirib xato qilmaydi).
- **Aktiv park = `trucks`** (TRUCK INFO, 157 ta). Hamma 6 varaq `all_trucks` jadvalida, LLM esa view'larni ko'radi:
  `trucks`, `rental_trucks`, `other_truck_lists` (CAC/DEMIR/New — asosan o'sha mashinalar), `inactive_trucks` (arxiv).
  Oldin hammasi bitta jadvalda edi va "nechta mashina" savoliga arxiv bilan birga 593 chiqardi.
- **Tozalash** (`app/loader.py`): "At MTP", "NEW YARD", "AT SHOP" kabi yozuvlar haydovchi emas, `location_note` ga
  ajratiladi. `417.0` → `417`, sanalar `YYYY-MM-DD`, `miles_until_pm` qayta hisoblanadi.
  Inactive Trucks varag'idagi surilgan ustunlar VIN bo'yicha to'g'rilanadi.
- **Qo'shimcha jadvallar:** `recent_issues` (Sheet103), `gps_tracking` (Insurance VIN — Samsara eksporti),
  `pti_checks` (PTI), `contacts` (Sheet45), `deductions_2022` (2022), `staff_days_off` (Sheet35).
- **Yuklanmagan varaqlar:** Fleet Live, Odometer, TR Detention (2022 yilgi shablon/eski ma'lumot) va bo'sh varaqlar.
  Jadvalga yangi varaq qo'shilsa, u avtomatik alohida jadval sifatida yuklanadi.
- **Xavfsizlik:** faqat bitta `SELECT`; SQLite authorizer yozishni bloklaydi; 3 soniya timeout; natija qatorlari cheklangan.
- **Tezlik:** router javoblari darhol; LLM savollari odatda 0.5–1.5 s. Groq 429 (limit) bersa SDK kutmaydi —
  darhol `LLM_FALLBACK_MODEL` dagi keyingi modelga o'tadi (Groq'da limit har model uchun alohida).
  Bir xil savol 10 daqiqa keshlanadi. Bot oxirgi 2 ta savol-javobni eslab qoladi (davomiy savollar uchun).

## Sozlamalar (`.env`)

| O'zgaruvchi | Ma'nosi |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather bergan token |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | OpenAI-mos API. Demo: Groq |
| `LLM_FALLBACK_MODEL` | vergul bilan modellar: asosiy model xato bersa yoki limitga tushsa darhol shularga o'tiladi |
| `COMPANY_NAME` | bot o'zini qaysi kompaniya yordamchisi deb tanishtiradi |
| `SPREADSHEET_PATH` | lokal .xlsx fayl. Fayl almashtirilsa, bot 30 soniya ichida o'zi qayta o'qiydi |
| `SHEET_URL` | Google Sheets havolasi (ixtiyoriy). Berilsa, har `REFRESH_MINUTES` da yangilanadi |
| `ALLOWED_USER_IDS` | botdan foydalanishi mumkin bo'lgan Telegram ID'lar (bo'sh = hamma) |
| `ADMIN_USER_IDS` | `/refresh` ni ishlata oladiganlar (bo'sh = hamma) |

O'z Telegram ID'ingizni bilish uchun: ruxsat ro'yxati yoqilgan bo'lsa, bot ruxsatsiz foydalanuvchiga ID'sini yozib yuboradi
(yoki @userinfobot).

## Production'ga o'tkazish

1. BotFather'da kompaniya nomi bilan yangi bot yarating, tokenini `.env` ga qo'ying.
2. OpenAI'ga o'tish uchun faqat `.env` o'zgaradi, kod o'zgarmaydi:
   ```
   LLM_BASE_URL=https://api.openai.com/v1
   LLM_API_KEY=sk-...
   LLM_MODEL=gpt-4.1-mini
   LLM_FALLBACK_MODEL=gpt-4.1
   COMPANY_NAME=Kompaniya nomi
   ```
3. Jadval Google Sheets'da bo'lsa, `SHEET_URL` ga havolani qo'ying (yoki Google Sheets API + service account).
4. `ALLOWED_USER_IDS` ni to'ldiring — aks holda botni topgan har kim ma'lumotlarni ko'ra oladi.
5. Serverda doimiy ishlashi uchun: VPS + `systemd` yoki Docker (`python main.py`). Kompyuter o'chsa demo bot to'xtaydi.
6. Boshqa kompaniyaning jadvali boshqacha tuzilgan bo'lsa, `app/loader.py` dagi varaq nomlari/ustun tartibini moslang
   (noma'lum varaqlar baribir avtomatik yuklanadi, lekin tozalanmagan holda).

## Eslatmalar

- **Groq bepul tarifi** har model uchun 8K token/daqiqa. Bitta LLM savol ~1.5K token, ya'ni 3 ta model bilan
  daqiqasiga ~15 savol. Tugmalar va unit/haydovchi qidiruvi token sarflamaydi.
- Groq model nomlari vaqt o'tib o'zgarishi mumkin. "model not found" xatosi chiqsa, console.groq.com → Models
  dan joriy modelni `.env` dagi `LLM_MODEL` ga yozing.
- Demo tugagach tokenlarni yangilang (BotFather → /revoke, Groq konsolida key'ni o'chiring), chunki ular chatda yozilgan.
