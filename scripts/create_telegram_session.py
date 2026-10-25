"""One-time setup: creates the MTProto admin session used to create deal groups.

Run it yourself in a terminal (never in chat, never in CI):
    python scripts/create_telegram_session.py

You will be asked for the phone number, the login code Telegram sends, and the 2FA password if set.
Nothing is printed except status lines. The session file is written to TELEGRAM_ADMIN_SESSION_PATH.
Use a dedicated Telegram account for MIDA, not a personal one.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from telethon import TelegramClient

# Run directly, this puts scripts/ on the import path, not the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402  (needs the path fix above)


async def main() -> int:
    settings = get_settings()
    if not settings.mtproto_configured:
        print("Set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_ADMIN_SESSION_PATH in .env first.")
        return 1
    if settings.telegram_api_id is None or settings.telegram_api_hash is None:
        return 1
    session = Path(settings.telegram_admin_session_path or "")
    session.parent.mkdir(parents=True, exist_ok=True)

    api_hash = settings.telegram_api_hash.get_secret_value()
    client = TelegramClient(str(session), settings.telegram_api_id, api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Already signed in as account id {me.id}. Nothing to do.")
            return 0
        prompt = "Phone number with country code (e.g. +998...): "
        phone = os.environ.get("TG_PHONE") or input(prompt).strip()
        await client.send_code_request(phone)
        code = input("Login code from Telegram: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except Exception as exc:  # telethon raises SessionPasswordNeededError when 2FA is enabled
            if "PASSWORD" not in type(exc).__name__.upper():
                raise
            password = input("2FA password: ")
            await client.sign_in(password=password)
        me = await client.get_me()
        print(f"Signed in. Session saved to {session}. Account id: {me.id}")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
