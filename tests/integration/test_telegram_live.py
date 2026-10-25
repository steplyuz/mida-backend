"""Live MTProto check. Needs real credentials; run with: pytest -m telegram_live."""

from __future__ import annotations

import os

import pytest

from app.integrations.telegram.mtproto import TelethonGateway

pytestmark = pytest.mark.telegram_live


@pytest.mark.skipif(
    not (os.environ.get("TG_API_ID") and os.environ.get("TG_API_HASH") and os.environ.get("TG_SESSION_PATH")),
    reason="requires TG_API_ID, TG_API_HASH and TG_SESSION_PATH",
)
async def test_admin_session_is_authorized() -> None:
    gateway = TelethonGateway(
        api_id=int(os.environ["TG_API_ID"]),
        api_hash=os.environ["TG_API_HASH"],
        session_path=os.environ["TG_SESSION_PATH"],
    )
    try:
        client = await gateway._connected()  # noqa: SLF001  (a connectivity check, not app code)
        assert await client.is_user_authorized()
    finally:
        await gateway.close()
