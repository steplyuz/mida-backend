# MIDA Backend

Transaction-trust platform backend: bitimlar (deals), to'lov holatlari, Telegram yozishmalari, dalillar va audit tarixi.

> **Holat:** Phase 1–9 tugallangan (qattiqlashtirish va yakuniy tekshiruv). Hujjatlar: `docs/RUNBOOK.md` (Windows PowerShell), `docs/SECURITY_REVIEW.md` (ochiq topilmalar), `docs/ACCEPTANCE.md` (qabul mezonlari, testlarga bog'langan). Phase 5 MTProto adapteri **jonli sinovdan o'tmagan** (credential yo'q). Phase 6 (xabar arxivi) webhook va fake update'lar bilan testlangan. To'lov, dalil va nizo modullari hali yo'q.

## Arxitektura (Phase 1)

| Qatlam | Joy | Vazifa |
|---|---|---|
| Config | `app/core/config.py` | `pydantic-settings`, `.env` va muhit o'zgaruvchilari |
| Logging | `app/core/logging.py` | JSON log, `request_id` korrelyatsiyasi |
| Middleware | `app/core/middleware.py` | `X-Request-ID`, access log, xavfsiz 500 javobi |
| Xatoliklar | `app/core/exceptions.py` | RFC 7807 `application/problem+json` |
| DB | `app/db/` | Async SQLAlchemy 2.0, SQLite PRAGMA (`foreign_keys=ON`) |
| API | `app/api/` | `/health`, `/ready`; `/api/v1` router skeleti |
| Migratsiya | `alembic/` | Async env, baseline `0001_baseline` |

## Domain (Phase 2)

| Komponent | Joy | Qoida |
|---|---|---|
| Pricing | `app/services/pricing_service.py` | Komissiya `product * bps / 10000`, half-up, faqat integer tiyin. 10% = 1000 bps |
| Deal raqami | `app/repositories/deal_repository.py` | `counters` jadvali, `UPDATE ... RETURNING`, bitta tranzaksiyada. Format `MIDA-000001` |
| State machine | `app/services/state_machine.py` | Har bir o'tish uchun ruxsat etilgan rollar jadvali, noma'lum o'tish rad etiladi |
| Optimistic lock | `DealRepository.update_status_if_version` | `WHERE version = :expected`, mos kelmasa 409 |
| Audit | `app/services/audit_service.py` | Business o'zgarish bilan bir tranzaksiyada; `correlation_id` = `X-Request-ID` |
| Append-only | `alembic` migratsiya 2 | `audit_logs` da UPDATE va DELETE trigger orqali bloklangan |

Narx snapshoti: bitim yaratilganda `service_fee_bps` va `terms_snapshot_json` saqlanadi. Keyinchalik default stavka o'zgarsa, eski bitim summasi o'zgarmaydi.

Muhim: client `service_fee` yoki `total` yubora olmaydi. `CreateDealInput` bunday maydonlarga ega emas.

## Deal API (Phase 4)

Bitimlar UUID `id` bilan murojaat qilinadi (ichki integer id tashqariga chiqmaydi). Barcha endpointlar `Authorization: Bearer` talab qiladi.

| Metod | Yo'l | Izoh |
|---|---|---|
| POST | `/api/v1/deals` | Yaratish. `Idempotency-Key` majburiy. `my_role` va `counterparty_telegram_user_id` yuboriladi |
| GET | `/api/v1/deals` | Faqat ishtirokchi bitimlari (admin: hammasi). `status`, `sort` (`created_at` / `-created_at`), `limit`, `cursor` |
| GET | `/api/v1/deals/{id}` | Ko'rish (ishtirokchi yoki admin) |
| PATCH | `/api/v1/deals/{id}` | `title`/`description` (faqat `created`, `awaiting_payment`). `expected_version` majburiy |
| POST | `/api/v1/deals/{id}/cancel` | Bekor qilish. `Idempotency-Key` majburiy |
| POST | `/api/v1/deals/{id}/confirm-receipt` | Xaridor tasdiqlaydi, bitim yakunlanadi. `Idempotency-Key` majburiy |
| GET | `/api/v1/deals/{id}/events` | Audit tarixi, cursor bilan |
| GET | `/api/v1/deals/{id}/participants` | Xaridor va sotuvchi |

Idempotency: kalit `(foydalanuvchi, operatsiya, kalit)` bo'yicha alohida saqlanadi va biznes o'zgarishi bilan bir tranzaksiyada yoziladi. Bir xil kalit + bir xil so'rov: asl natija qaytadi (`Idempotent-Replayed: true`). Bir xil kalit + boshqa so'rov: `409 idempotency-key-reused`. Bir vaqtdagi bir xil kalitli so'rovlar bitta bitim yaratadi (test bilan tasdiqlangan).

Kirish nazorati: bitimga aloqasi bo'lmagan foydalanuvchi uchun "topilmadi" (404) javobi, mavjud bo'lmagan bitim bilan bir xil. Client `buyer_user_id`, `service_fee_*` yoki `total_*` yubora olmaydi (422).

Cursor: ichida sort maydonlari bor; boshqa sort bilan qayta ishlatilsa yoki o'zgartirilsa 422.

Cheklovlar: to'lov va yetkazib berish o'tishlari (`awaiting_payment` dan keyin) hozircha HTTP endpointga ega emas, faqat servis darajasida (testlar `SYSTEM` va sotuvchi aktyorlari orqali o'tkazadi). Telegram guruh, dalil va nizo endpointlari keyingi bosqichlarda. Idempotency yozuvlarini tozalash (retention) Phase 9 da.

## Nizolar va dalillar (Phase 8)

Nizo ochish: faqat bitim tomonlari (xaridor yoki sotuvchi), faqat `payment_confirmed`, `delivery_in_progress` yoki `awaiting_buyer_confirmation` holatida. Nizo ochilganda bitim `disputed` ga o'tadi, ikkalasi bir tranzaksiyada yoziladi. Bitta bitimda bir vaqtda faqat bitta yechilmagan nizo bo'ladi, buni bazadagi qisman unikal indeks kafolatlaydi (parallel ochish testi bilan tasdiqlangan).

Qaror oqimi: `open` → admin `assign` (`under_review`) → faqat tayinlangan admin `resolve`. Yechim ikkita: `buyer_wins` (bitim bekor) yoki `seller_wins` (bitim yakunlanadi). Qaror sababi kamida 20 belgi va auditga yoziladi. Qaror faqat yozuv: pul harakati va huquqiy oqibat bu yerda yo'q.

Dalillar:
- Fayl yuklash: xom body (`Content-Type` haqiqiy tur, `X-Filename`, `X-Evidence-Label`). Ruxsat etilgan turlar: PNG, JPEG, PDF. Deklaratsiya va haqiqiy imzo (magic bytes) mos kelmasa rad etiladi. SVG va HTML rad etiladi.
- Hajm `EVIDENCE_MAX_BYTES` bilan cheklangan (default 10 MB), Content-Length va oqim bo'yicha tekshiriladi, fayl saqlanishidan oldin.
- Saqlash: fayl nomi ishlatilmaydi, kalit server tomonidan generatsiya qilinadi (32 hex). Yozish atomik. DB xato bersa, yetim fayl o'chiriladi.
- SHA-256 saqlanadi. Yuklab olishda hash qayta tekshiriladi, buzilgan fayl berilmaydi.
- Yuklab olish har doim `attachment`, `nosniff` va `no-store` bilan. Yuklangan hech narsa brauzerda ochilmaydi.
- Telegram xabari dalil sifatida havola qilinadi (fayl emas), faqat shu bitimga tegishli xabar.
- Dalil qo'shish: faol bitimda yoki ochiq nizo paytida. Yopilgan va nizosiz bitim dalillari muzlatiladi.

Endpointlar: `POST /api/v1/deals/{id}/disputes`, `GET .../disputes`, `GET /api/v1/disputes/{id}`, `POST /api/v1/disputes/{id}/assign` va `/resolve` (admin), `POST /api/v1/deals/{id}/evidence/files|notes|message-references`, `GET .../evidence`, `GET /api/v1/evidence/{id}/content`.

Cheklovlar:
- PDF ichidagi faol kontent (JavaScript va boshqalar) skanerlanmaydi. Shuning uchun PDF faqat yuklab olish sifatida beriladi, hech qachon ko'rsatilmaydi.
- Fayl saqlash hozircha mahalliy disk. Ishlab chiqarishda S3 bilan almashtiriladi, interfeys tayyor.
- Qaror uchun ikki kishilik tasdiq (four-eyes) hali yo'q.
- Qaror hozircha huquqiy oqibatsiz. Shartlar Terms of Service bilan kelishilishi kerak.

## To'lov qatlami (Phase 7)

> **Muhim:** bu bosqichda haqiqiy pul oqimi **yo'q**. Hech qanday bank yoki to'lov provayderi ulanmagan. MIDA hech qachon pul ushlab turgan yoki sotuvchiga o'tkazgan deb ko'rsatmaydi. Provayder ulanmaguncha bitim holati faqat "to'lov tekshirilmoqda" yoki "to'lov tasdiqlandi" (provayder faktlari asosida) bo'lishi mumkin.

Arxitektura:
- `PaymentProvider` interfeysi (`app/integrations/payments/base.py`). Ilova faqat shu interfeysga bog'liq.
- Mock provayder faqat `MOCK_PAYMENT_PROVIDER=true` va `APP_ENV != production` bo'lganda mavjud. Production'da konstruktor ham, sozlamalar validatsiyasi ham uni rad etadi. Webhook imzosi `MOCK_PAYMENT_WEBHOOK_SECRET` bilan HMAC-SHA256.
- Provayder chaqiruvi DB tranzaksiyasi ichida emas. Boshlash uch fazali: (A) PENDING yozuv, (B) provayder chaqiruvi bir xil kalit bilan, (C) tranzaksiya ID va bitim holatini yangilash. Jarayon B dan keyin qulasa, xuddi shu kalit bilan qayta urinish C ni davom ettiradi.
- Bitim holati faqat provayderdan kelgan **imzolangan** webhook asosida o'zgaradi. Frontend yuborgan "to'landi" ma'lumoti e'tiborga olinmaydi.

Qoidalar:
- Webhook: imzo avval tekshiriladi, keyin body. `event_id` takrorlansa qo'llanmaydi.
- Summa yoki valyuta mos kelmasa to'lov `requires_verification` holatida qoladi va bitim o'zgarmaydi.
- Tartibsiz hodisa (masalan, tasdiqlangan to'lov uchun "failed") qo'llanmaydi, audit bilan belgilanadi.
- Noma'lum tranzaksiya ID qayd etiladi, lekin hech qanday bitimga biriktirilmaydi.
- Bir bitimda bir vaqtning o'zida faqat bitta ochiq to'lov bo'ladi. Muvaffaqiyatsiz to'lovdan keyin xaridor yangi kalit bilan qayta urinishi mumkin.
- Provayder holati bilan MIDA o'rtasidagi nomuvofiqlik `GET /api/v1/admin/payments/reconciliation` da ko'rinadi. Reconciliation hech qachon holatni o'zi o'zgartirmaydi.
- Refund: `request_refund` hozircha `501 unsupported-operation` qaytaradi.

Endpointlar: `POST /api/v1/deals/{id}/payments` (xaridor, `Idempotency-Key` majburiy), `GET .../payments`, `GET .../payment-status`, `POST /api/v1/payments/webhook/{provider}`.

Ochiq masalalar (provayder ulanishidan oldin hal qilinishi shart):
- Haqiqiy provayder tanlanmagan, shartnoma va escrow/delayed-settlement imkoniyati tekshirilmagan.
- Qonuniy talablar (pul o'tkazish litsenziyasi, qaysi hisobda pul turishi) yuridik maslahat bilan belgilanishi kerak.
- Admin qo'lda to'lov qayd etish endpointi ataylab qo'shilmagan: u haqiqiy provayder tasdig'i bilan adashtirilishi mumkin.

## Xabar arxivi (Phase 6)

Guruh xabarlari Telegram Bot API webhook orqali qabul qilinadi: `POST /api/v1/telegram/webhook`. So'rov `X-Telegram-Bot-Api-Secret-Token` sarlavhasi bilan himoyalangan (`TELEGRAM_WEBHOOK_SECRET`, kamida 32 belgi). Sir sozlanmagan bo'lsa webhook 503 qaytaradi (fail closed). Taqqoslash `secrets.compare_digest` bilan.

Qoidalar:
- Faqat `group` va `supergroup` xabarlari arxivlanadi. Botning o'z xabarlari va nomalum chatlar arxivlanmaydi.
- Xabar faqat chat id orqali bitimga bog'lanadi (`deals.telegram_chat_id`). Boshqa bitimga o'tib qolish mumkin emas (test bilan tasdiqlangan).
- Redelivery: `update_id` bo'yicha qabul qilingan update qayta ishlanmaydi. Bir xil `(chat, message_id)` yangi update bilan kelsa ham bitta qator bo'ladi.
- Tahrir: asl matn `message_revisions` ga yoziladi, xabar hozirgi matnni saqlaydi. Har tahrir `revision_no` oladi. Bir xil matnli tahrir yozilmaydi.
- Ishlov berishda xato bo'lsa, receipt ham qaytariladi (bitta tranzaksiya). Telegram update'ni qayta yuboradi va u yo'qolmaydi.
- Ishtirokchi bo'lmagan yuboruvchi xabari arxivlanadi, lekin `sender_user_id` bo'sh qoladi.
- Media uchun faqat metadata saqlanadi (`file_unique_id`, hajm, MIME, fayl nomi basename). Telegram `file_id` saqlanmaydi.
- Ko'rish: faqat bitim ishtirokchilari va admin. Javoblarda chat id yoki Telegram user id yo'q.

Endpointlar: `GET /api/v1/deals/{id}/messages` (cursor pagination), `GET /api/v1/deals/{id}/messages/{message_id}/revisions`.

Muhim cheklovlar:
- **O'chirilgan xabarlar Bot API orqali kelmaydi.** Shuning uchun o'chirish tarixi hozir faqat servis darajasida (kelajakdagi MTProto manbai uchun) yozilishi mumkin. Telegramda o'chirilgan xabar qayta tiklanmaydi deb hisoblanmaydi.
- Bot guruh xabarlarini faqat privacy mode o'chiq bo'lsa yoki bot guruh admini bo'lsa ko'radi. Bu BotFather va guruh sozlamalarida qo'lda sozlanadi.
- Xabar matni HTML sifatida emas, oddiy matn sifatida saqlanadi. Klient tomoni escape qilishi shart.
- Xabarlar shaxsiy ma'lumot. Retention siyosati (qancha saqlash, kim o'chira oladi) hali belgilanmagan va Terms of Service bilan kelishilishi kerak.

## Telegram guruhi va outbox (Phase 5)

Bitim yaratilganda `create_telegram_group` outbox eventi shu tranzaksiyada yoziladi. Worker eventni lease bilan oladi (bir vaqtda ikki worker bir eventni olmaydi) va quyidagi tartibda bajaradi:

1. Guruh yaratiladi, nomi `MIDA-000001 | Mahsulot` (`telegram_group_status` = `pending`, so'ng `created`). Chat id darhol saqlanadi, shuning uchun qayta urinish ikkinchi guruh yaratmaydi.
2. Har bir tomonga o'zining bir martalik invite linki yaratiladi (`usage_limit=1`, muddati `TELEGRAM_INVITE_TTL_SECONDS` bilan, default 86400 s). Hech kim to'g'ridan-to'g'ri qo'shilmaydi. Link faqat shu tomonga ko'rinadi va hech qachon logga yozilmaydi.
3. Logo alohida `set_group_avatar` eventi sifatida o'rnatiladi. Logo xatosi guruhni bekor qilmaydi.

Xato klassifikatsiyasi: `rate_limit` (server `retry_after` ni hurmat qilinadi) va `temporary_network` qayta uriniladi, eksponensial backoff va jitter bilan, `OUTBOX_MAX_ATTEMPTS` gacha. Qolgan xatolar (`configuration`, `authentication`, `permission`, `group_creation`) darhol `failed` holatiga o'tadi. Admin `POST /api/v1/deals/{id}/telegram-group/retry` yoki `POST /api/v1/admin/outbox/{id}/retry` orqali qayta navbatga qo'yishi mumkin.

Admin endpointlari: `GET /api/v1/admin/outbox`, `GET /api/v1/admin/integrations/telegram` (konfiguratsiya va navbat holati, sir yo'q).

Ishga tushirish: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_ADMIN_SESSION_PATH` sozlansa worker avtomatik yoqiladi (`TELEGRAM_WORKER_ENABLED=true/false` bilan majburlash mumkin). Session fayli `.gitignore` da, uni alohida OS foydalanuvchisi o'qishi kerak. Bir martalik login (telefon, OTP, 2FA) server tomonida qo'lda bajariladi; login ma'lumotlari logga yozilmaydi.

Muhim cheklovlar:
- MTProto `create_supergroup` uchun idempotency kaliti yo'q. Worker guruh yaratilgan va chat id saqlanmagan orada qulasa, guruh yetim qolishi mumkin. Bu `telegram.group_create_started` audit eventida ko'rinadi, operator uni qo'lda tekshirishi kerak.
- Telegram Bot API guruh yaratish uchun ishlatilmaydi.
- Haqiqiy Telegram sinovi: `pytest -m telegram_live` (credential kerak). Standart test to'plami bu sinovni o'tkazib yuboradi.
- Xabar arxivi (Phase 6) hali yo'q.

## Autentifikatsiya (Phase 3)

Oqim: Mini App `initData` ni `POST /api/v1/auth/telegram` ga yuboradi, server HMAC-SHA-256 imzosini tekshiradi va JWT access token qaytaradi.

- Imzo: rasmiy Telegram algoritmi (`WebAppData` salt, `hmac.compare_digest`). Imzo tekshirilmaguncha hech qanday maydon o'qilmaydi.
- `auth_date` muddati `TELEGRAM_INITDATA_MAX_AGE_SECONDS` (default 300 s). Kelajakdagi vaqt 60 s dan ko'p bo'lsa rad etiladi.
- Dublikat kalit, noto'g'ri `user` JSON, bool/manfiy `id` rad etiladi.
- Token: HS256, `iss`/`aud`/`typ`/`exp` tekshiriladi, muddati `ACCESS_TOKEN_TTL_SECONDS` (default 900 s). Tokenda rol yo'q.
- Har so'rovda rol va `is_active` bazadan qayta o'qiladi: deaktivatsiya yoki rol o'zgarishi token muddati tugashini kutmaydi.
- Rol faqat server tomonida o'zgaradi. `PATCH /users/me` `role` ni qabul qilmaydi (422).
- Admin marshrutlari `AdminUserDep` orqali himoyalangan.
- Production'da `TELEGRAM_BOT_TOKEN` va `APP_SECRET_KEY` (32+ belgi) bo'lmasa ilova ishga tushmaydi.
- Dev bypass yo'q: lokal testlar ham haqiqiy imzolangan `initData` ishlatadi.

Xato javoblari: `401` (`WWW-Authenticate: Bearer`), `403` (`account-disabled`, `permission-denied`), `503` (`service-not-configured`). Xato matnida `initData` yoki hash aks etmaydi.

Cheklov: rate limiting hali yo'q (auth endpoint uchun alohida interfeys keyingi bosqichda). Token bekor qilish (revocation) ro'yxati ham yo'q; deaktivatsiya bazada darhol ishlaydi.

## Ishga tushirish (Windows PowerShell)

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Tekshirish: `http://localhost:8000/health`, `/ready`, Swagger: `/docs` (faqat `APP_ENV != production`).

Uv bilan (tavsiya etiladi): `uv sync --extra dev`, so'ng `uv run pytest`, `uv run ruff check .`, `uv run mypy`.

## Testlar

```powershell
pytest                 # default: telegram_live belgisi bilan testlar o'tkazib yuboriladi
ruff check .
ruff format --check .
mypy
```

Test muhiti `.env` faylini o'qimaydi (`_env_file=None`), shuning uchun mahalliy sozlamalar testlarga ta'sir qilmaydi.

## Konfiguratsiya qoidalari

- Nisbiy SQLite yo'li `backend/` papkasiga nisbatan hisoblanadi, ishga tushirish joyiga emas.
- `APP_ENV=production` bo'lsa: `DEBUG=true` yoki `MOCK_PAYMENT_PROVIDER=true` bilan ilova ishga tushmaydi.
- `DATABASE_URL` log'ga yozilmaydi (keyinchalik credential bo'lishi mumkin).
- Pul qiymatlari faqat integer tiyin bilan saqlanadi; `float` ishlatilmaydi.

## Xavfsizlik (hozircha)

- Xatolik javoblarida traceback yoki ichki xabar yo'q; 500 javobida faqat `request_id` bor.
- Validatsiya xatolari kiritilgan qiymatni qaytarmaydi.
- Session/token/invite-link loglarga yozilmaydi (hozircha bunday ma'lumot yo'q).

## Ma'lum cheklovlar va keyingi bosqichlar

- Telegram (aiogram, Telethon) va to'lov provayderi interfeysi: Phase 3, 5, 7.
- Domain jadvallari (users, deals, ...) va pricing: Phase 2.
- Uvicorn access log'i JSON formatga o'tkazilmagan; production'da `--no-access-log` tavsiya etiladi.
- Starlette `TestClient` httpx deprecation ogohlantirishi beradi; funksional ta'sir yo'q.
- Rate limiting interfeysi va idempotency: keyingi bosqichlarda.

## Admin va MIDA ID

- Admin: `ADMIN_TELEGRAM_IDS=123456789,987654321` (vergul bilan). Ro'yxatdagi Telegram ID bilan kirgan foydalanuvchi `admin` roliga o'tadi (mavjud foydalanuvchi ham keyingi kirishda).
- MIDA ID: `users.mida_id`, 8 xonali son, 10000001 dan boshlanadi, 99999999 gacha (`MIDA_ID_START` — `user_repository.py`). Ichki `users.id` o'zgarmagan.
- Migratsiya: `alembic upgrade head` (mavjud foydalanuvchilarga ham MIDA ID beradi).

## Bot bildirishnomalari

Bitim yaratilganda (qarshi tomonga), guruh tayyor bo'lganda (har bir tomonga o'z linki bilan) va holati o'zgarganda (to'lov, yetkazish, nizo, bekor qilish; o'zgartirgan foydalanuvchidan tashqari ikkala tomonga) bot foydalanuvchining shaxsiy chatiga xabar yuboradi. Xabar `notify_user` outbox hodisasi orqali, biznes tranzaksiya bilan birga yoziladi va worker tomonidan yuboriladi (qayta urinish bilan).

- Kerak: `TELEGRAM_BOT_TOKEN`. Worker shu bilan ham ishga tushadi.
- Foydalanuvchi botda kamida bir marta **Start** bosgan bo'lishi kerak, aks holda Telegram xabarga ruxsat bermaydi (xabar o'tkazib yuboriladi, xato sifatida qolmaydi).
- `TELEGRAM_MINIAPP_URL` berilsa, xabarda bitimni ochadigan tugma bo'ladi.


## Guruh boti (alohida bot)

Bitim guruhida ishlaydigan bot notification botidan alohida: o'z tokeni (`GROUP_BOT_TOKEN`), o'z webhook sirri (`GROUP_BOT_WEBHOOK_SECRET`, kamida 32 belgi) va webhook manzili `POST /api/v1/telegram/group-bot/webhook`. Sirr sozlanmagan bo'lsa webhook 503 qaytaradi.

Bot admin tomonidan guruhga qo'shiladi. Qo'shilganda bitim shartlari va qoidalar guruhga joylanadi (bir marta, qayta qo'shilsa ham takrorlanmaydi). Bitim holati o'zgarganda holat xabari guruhga boradi. Buyruqlar:

- `/holat`: bitim holati. Faqat xaridor va sotuvchi javob oladi, boshqalar uchun bot jim.
- `/havola`: shaxsiy bir martalik havola faqat shaxsiy chatda yuboriladi. Guruhga havola hech qachon yozilmaydi. Birinchi marta botga shaxsiy chatda `/start` yozish kerak.
- `/yordam`: buyruqlar ro'yxati.

Cheklovlar: qoidalar matni (`GROUP_RULES` shabloni) hozircha umumiy, uni kompaniya qoidalariga moslashtirish kerak. Bot guruhda barcha xabarlarni ko'rishi uchun BotFather'da privacy mode o'chiq bo'lishi yoki bot admin bo'lishi kerak. Haqiqiy Telegram botida sinovdan o'tkazilmagan.
