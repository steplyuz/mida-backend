# Administrator panel: setup and operating rules

## What the panel does
- Dashboard: counters only (users, deal statuses, volume, fees, pending work, failed Telegram tasks).
- Payments: a bank transfer is recorded by one administrator and approved by a second one.
- Seller payouts: requested after a deal completes, approved by a second administrator, then marked paid with the bank reference.
- Users: search, block, unblock. Blocking takes effect on the next request.
- Inquiries: the inbox for the public site form and the Mini App support threads; replies are kept.
- Disputes: read-only list. Assign and resolve stay in the Mini App flow.
- Deal archive: the group message archive and the event trail of one deal.

## What the system does NOT do
- It does not move money. "Approved" and "paid" are records of bank actions done by people, outside the system.
- It does not refund buyers. Refunds are handled by hand and are not recorded here.
- Visitors on the public site cannot read replies in this version; the admin answers through the contact they gave.

## One-time setup
1. Set the environment (backend `.env`):
   ```
   MANUAL_PAYMENTS=true
   CORS_ORIGINS=https://admin.example.uz
   TELEGRAM_BOT_TOKEN=<the bot's own token>
   ```
   Do not enable `MOCK_PAYMENT_PROVIDER` in production.
2. BotFather: `/setdomain` for the bot, set to the panel's host (for example `admin.example.uz`). The Login Widget refuses other domains.
3. Create the administrators. Each person must first sign in to MIDA in the Mini App, then run on the server:
   ```
   python scripts/grant_admin.py --telegram-id <ID>
   ```
   There is no HTTP route for granting roles, on purpose. Use at least two administrators: the four-eyes rules need two different people for every payment and payout.
4. Build and host the panel (`npm run build`) on its own domain over HTTPS, with its own origin. The API must allow that origin in `CORS_ORIGINS`.

## Rules the code enforces (and tests check)
- A payment cannot be approved by the administrator who recorded it.
- A payout cannot be approved, or marked paid, by the administrator who requested it.
- One bank reference cannot cover two payments, or two payouts.
- A payout exists only for a completed deal whose payment is confirmed. The amount is the product price; the MIDA fee stays with MIDA.
- A rejected payout can be requested again; the history stays in the audit log.
- The honeypot field is ignored and stores nothing. Public inquiries are limited per address (`RATE_LIMIT_INQUIRY_PER_HOUR`).
- Administrator sign-in refuses non-administrators with the same answer as "not registered": nothing about other accounts is revealed.

## Operating advice
- Restrict the panel to known networks where possible (for example a VPN or an IP allowlist at the hosting layer). It is a high-value target.
- Review the audit log (`audit_events`) after each batch of payouts. Every admin action records the actor.
- Do not share the bot token. If it leaks, revoke it in BotFather and update `.env`.
- Back up the database before large payout batches (`scripts/backup_db.py`).
