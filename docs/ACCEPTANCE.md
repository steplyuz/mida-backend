# Acceptance checklist (spec section 21) — status as of Phase 9

Each line states what the evidence is. "Partial" means it is not done as written.

- [x] FastAPI starts — `uvicorn app.main:app`; smoke-tested each phase.
- [x] Async SQLite connection works — `tests/integration/test_database.py`; all DB access is async.
- [x] Alembic migration succeeds — `alembic upgrade head`, downgrade round-trip, `alembic check` in `test_schema_matches_models_and_has_one_head`.
- [x] Deal number is unique — `test_parallel_creation_yields_unique_gapless_numbers`.
- [x] 10% commission computed in the backend — `tests/unit/test_pricing.py`, `test_create_deal_stores_server_computed_price`.
- [x] Money stored as integer tiyin — columns are BigInteger; `float` never used for money.
- [x] State machine blocks invalid transitions — `tests/unit/test_state_machine.py`, `test_invalid_transition_is_rejected_and_state_is_unchanged`.
- [x] Telegram initData verified server-side — `tests/unit/test_telegram_auth.py`.
- [x] Role and resource permissions work — `tests/integration/test_deal_api.py` (BOLA), `tests/security/test_access_policy.py`.
- [x] Audit log records important actions — `tests/integration/test_deal_api.py::test_events_are_visible_to_parties_in_order`, payment and dispute audit tests.
- [x] Idempotency protects repeated actions — `tests/integration/test_idempotency_concurrency.py`.
- [~] Telegram MTProto adapter exists — **Partial.** Exists and is import-checked; not verified against live Telegram (no credentials). The live test is excluded by default.
- [x] Group creation is async and retryable — `tests/integration/test_telegram_worker.py`.
- [x] Logo failures recorded separately — `test_logo_failure_does_not_block_the_group`.
- [~] Telegram message archive pipeline exists — **Partial.** Webhook ingestion, dedup, edits and revisions are tested. Deletions are not observable through the Bot API.
- [x] Payment provider interface ready — `app/integrations/payments/base.py`.
- [x] Mock payment cannot run in production — `test_mock_provider_refuses_to_exist_in_production`, settings validation.
- [x] Disputes and evidence modules exist — `tests/integration/test_disputes_api.py`.
- [x] Tests do not depend on real Telegram or real payments — `telegram_live` marker excluded from the default run.
- [x] README and `.env.example` ready — `.env.example` has no secret values (scanned).
- [x] No real credential is in the repository — secret scan in Phase 9 found none; demo values are labelled placeholders.
- [~] Non-working integrations are not presented as working — **Partial by design.** Telegram live and payments are stated as not connected in README, runbook and this file.

Summary: 20 items done, 3 partial (the three marked above). Nothing in the list is marked done without a test or a recorded smoke run behind it.
