# Security review — MIDA backend (Phase 9)

Scope: the backend as built through Phase 9. Honest status, including what is not done. This is an engineering review, not an external audit or a legal opinion.

## Controls in place, and where they are tested

| Area | Control | Test evidence |
|---|---|---|
| Authentication | Telegram initData HMAC-SHA-256, constant-time compare, `auth_date` freshness, duplicate-key rejection | `tests/unit/test_telegram_auth.py`, `tests/integration/test_auth_api.py` |
| Sessions | HS256 JWT, `iss`/`aud`/`typ`/`exp` required, short lifetime (15 min); `alg: none` rejected | `tests/unit/test_security.py` |
| Revocation | Role and active status re-read on every request; deactivation is immediate | `test_deactivation_takes_effect_before_token_expiry` |
| Access policy | Every operation is public or bearer-protected; public list is reviewed | `tests/security/test_access_policy.py` |
| Admin | Admin dependency runs before any lookup; members get a uniform 403 | `test_admin_operations_refuse_ordinary_members` |
| Object access | Non-participants get the same 404 as a missing object (no enumeration of deals) | `test_missing_and_foreign_deals_are_indistinguishable` |
| Money | Integer tiyin only; fee computed server-side; client cannot supply fee or total | `tests/unit/test_pricing.py`, `test_client_cannot_name_internal_parties_or_fees` |
| Payments | Provider webhook signature checked before any field is read; event ids applied once; amount and currency checked | `tests/unit/test_payments_core.py`, `tests/integration/test_payment_api.py` |
| Mock provider | Refuses to exist in production (constructor and settings) | `test_mock_provider_refuses_to_exist_in_production` |
| Idempotency | Keys scoped per user and operation; same key with different body is refused; races resolved to the winner | `tests/integration/test_idempotency_concurrency.py` |
| Concurrency | Compare-and-set on deal and dispute versions; one open dispute per deal enforced by a partial unique index | `tests/integration/test_dispute_concurrency.py` |
| Audit | Append-only at the database (triggers block UPDATE and DELETE) | `test_audit_rows_cannot_be_updated_or_deleted` |
| Uploads | Declared type must match file signature; SVG and HTML refused; size capped before storage; server-generated storage keys | `tests/unit/test_evidence_rules.py`, `tests/integration/test_disputes_api.py` |
| Downloads | Attachment only, `nosniff`, `no-store`; SHA-256 re-verified before serving | `test_tampered_file_on_disk_is_not_served` |
| Webhooks | Telegram secret header and provider HMAC compared in constant time; unknown providers 404 | `tests/integration/test_webhook_api.py` |
| Rate limits | Login per address, deal creation (and counterparty lookups) per user, payment start per user; `Retry-After` on 429 | `tests/security/test_access_policy.py` |
| Errors | Problem+json everywhere; no tracebacks or internal text in bodies | `test_error_bodies_never_contain_tracebacks` |
| Headers | `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `no-store` on every response | `test_responses_carry_the_baseline_security_headers` |
| Secrets | Settings never printed (`SecretStr`); production refuses to start without strong secrets; `.env` and session files ignored by git | `test_secrets_are_not_printed`, production validation tests |
| Logs | No invite links, Telegram ids, database URLs or tokens logged (checked in tests for invite links) | `test_party_who_cannot_be_added_gets_a_one_use_invite_only_they_can_see` |

## Open findings (not fixed; each has a decision attached)

1. **Counterparty enumeration (medium).** A user can ask whether a Telegram id has signed in, by creating a deal with it. Mitigated, not removed: deal creation and lookups are rate-limited per user (30 per hour by default). The real fix is invitation codes: random, shareable, not derived from the Telegram id. Decision needed before public launch.
2. **Rate limiter is per process (medium).** With several uvicorn workers each keeps its own counters, so the effective limit multiplies. Production needs a shared store behind the same interface. Single-worker deployments are fine.
3. **Client address trust (medium).** Login limits use the TCP peer address. Behind a proxy this is the proxy's address unless uvicorn is configured to trust it (runbook section 3). Misconfiguration causes self-inflicted lockouts, not a bypass.
4. **PDF active content not scanned (medium).** Signatures are checked, but PDFs are not scanned for embedded scripts or malware. PDFs are served only as attachments. A malware scanning step belongs before any wider rollout.
5. **No four-eyes on disputes (medium).** One assigned admin decides. A second approver is a product decision.
6. **Admin role is set in the database (operational).** There is no endpoint to grant admin. This is intentional (no privilege escalation over HTTP), but it is a manual, audited operator step that must be written into the operations procedure.
7. **Telegram MTProto adapter unverified live (high for go-live).** Written against the Telethon 1.x API and import-checked. It has not talked to Telegram. The live test exists but needs real credentials.
8. **Bot cannot see deleted messages (functional).** Deletion history is not recorded from the Bot API. Stated in the README; a retention and legal decision is needed before relying on deletion evidence.
9. **Retention not scheduled (operational).** Retention functions exist and are tested; nothing runs them. Audit retention is a legal decision and is not implemented.
10. **Evidence and backups hold personal data (operational).** Backups must be encrypted at rest and access-controlled; the runbook says so but enforcement is outside the code.
11. **Token revocation is by expiry only (low).** Deactivation is immediate, but a stolen token stays valid until it expires (max 15 minutes), since there is no revocation list.
12. **Payment provider not connected (blocking for money).** Nothing here moves money. Escrow or delayed-settlement, and the legal treatment of held funds, are outside this codebase until a bank contract and counsel review exist.

## Things that were found and fixed during the build

- Verifier matched webhook headers case-sensitively, rejecting valid signatures (Phase 7).
- Dispute admin check ran after object lookup, so members got 404 instead of a uniform 403 (Phase 9).
- Retry-After was one second too long (Phase 9).
- A parallel payment start could corrupt the deal state narrative with a false "closed deal" audit (Phase 7).
- Reconciliation output used `__dict__` on a slotted dataclass, returning 500 (Phase 7).
