# Governance Desk — remaining work checklist

Status legend: `[x]` implemented and tested · `[~]` implemented, awaiting external configuration ·
`[ ]` not implemented · `[!]` blocked by a decision.

Baseline verified on 2026-10-11 before Phase 3 (uncommitted Phase 2 working tree, branch `main`,
last commit `e99504a`): backend 128 passed / 2 skipped. The running local server (started 9 Oct)
is serving Phase 1 code; the real workspace database (`.sites-runtime/preview/preview.sqlite`,
6 member profiles, 1 audit event, 1 session, 0 stored files) is at revision `0001_baseline`.

## Known gaps found during inspection

* Authentication is the development-only local-preview sign-in: anyone who opens the page is Owner.
* No CSRF token (relies on SameSite=Strict + Origin check), no login rate limiting, no sign-out.
* Sessions have an absolute expiry only; no idle timeout, no revocation, no per-user session list.
* No way to deactivate a team member (memberships have a status, but no API changes it).
* SQLite only; writes serialised with an in-process lock (single process assumption).
* Invoice-number and payment-reference uniqueness are application checks only (no DB constraint).
* Every frontend action reloads the whole workspace (`GET /api/v2`); audit view limited to 300.
* Documents: whole file read into memory; no scanning ("Not scanned"); no quarantine; local disk only.
* Reminders: in-process loop with a lease; no durable job records, retries or failure visibility.
* Email: synchronous Resend call inside the request; no templates, preferences or delivery log.
* Audit: no actor/resource columns beyond Phase 2's membership id; rows are mutable; no chain.
* Operations: no container image, backup/restore tooling, metrics or deployment guide.

## Phase 3 — Authentication (done)
- [x] Built-in password sign-in (argon2id), sign-out, sign-out everywhere — `app/services/auth_service.py`
- [x] Sessions: absolute + idle expiry, server-side revocation, per-session revocation
- [x] Secure cookies (`__Host-` + Secure in staging/production), CSRF token (double submit, bound to the session)
- [x] Login throttling (per account and per client address, database-backed); reset-request throttling
- [x] Invitations / account activation, password reset, password change (single-use hashed tokens)
- [x] Member deactivation/reactivation revokes sessions; last-owner protection
- [x] Security events + organization audit entries for authentication
- [x] Preview login impossible in staging/production; disabled in password mode
- [x] Sign-in, activation and reset screens; sign-out and team account controls in the React app
- [x] Owner-set temporary passwords at member creation + server-enforced forced change on first use — migration `0005_forced_password_change`
- [x] Tests: `tests/test_auth.py` (42), `tests/e2e/test_password_sign_in.py` (2 browser scenarios)
- [x] Verified live on the real workspace (not just tests): restarted the server in `password` mode, bootstrapped the real Owner account via activation link, created a member with a temp password through the real UI, confirmed the forced-change gate blocks the API until it's changed
- [ ] MFA (TOTP) — not implemented; recommended before production use

## Phase 4 — PostgreSQL (done)
- [x] Dialect-aware repositories (JSONB, `seq` ordering column, upserts, JSON functions) — `app/database/dialect.py`
- [x] Migrations 0001–0004 run on PostgreSQL and SQLite; advisory lock for concurrent migrations
- [x] DB-level invoice-number and payment-reference uniqueness (migration 0004)
- [x] Connection pool + statement/lock timeouts; no in-process lock on PostgreSQL; busy errors → 409
- [x] SQLite → PostgreSQL transfer with full reconciliation (`python -m app.cli transfer-to-postgres`)
- [x] Multi-process tests (invoice numbers, payment references, optimistic locking, leases, migrations)
- [x] Whole suite and the 325 compatibility checks pass on PostgreSQL; CI runs both databases (not yet run on GitHub)
- [x] Copy of the real workspace transferred and reconciled (`ok: true`)

## Phase 5 — Frontend performance
- [ ] Incremental change feed instead of full reloads; stale-response protection
- [ ] Server-side search; paginated audit trail; deduplicated requests
- [ ] Tenant-boundary tests for the new endpoints

## Phase 6 — Documents
- [ ] Storage interface (local + S3-compatible), streaming upload/download
- [ ] Scanner interface (ClamAV clamd), quarantine, scan states; honest "not scanned" without scanner
- [ ] Metadata, audit, retention policy; backup/restore of files

## Phase 7 — Background jobs
- [ ] Durable `jobs` table, retries with backoff, idempotency keys, dead-letter state
- [ ] Single scheduler via lease; worker process; failed-job visibility

## Phase 8 — Notifications
- [ ] Templates, delivery log, preferences, providers (outbox sink, Resend, SMTP)
- [ ] Event notifications (deadlines, meetings, requests, reviews, submissions, invoices)

## Phase 9 — Security & audit hardening
- [ ] Append-only audit with hash chain; actor/resource/change details
- [ ] Segregation-of-duties rules; security review document

## Phase 10 — Deployment & operations
- [ ] Dockerfile, compose file, proxy/TLS guide, metrics, backup + tested restore, runbook

## Phase 11 — Data model review
- [ ] Normalisation assessment; targeted constraints

## Phase 12 — Final verification
- [ ] Full regression and production-readiness report
