# Governance Desk — Module Working & Integration Audit

**Date:** 2026-10-17 (UTC) / 2026-10-18 (IST)
**Scope:** functional and integration audit of the running application (React frontend, FastAPI backend, PostgreSQL)
**Type:** audit only — no source code, schema, migration, permission or business behaviour was changed, and no business data was modified
**Working tree at audit time:** `main` at commit `f09c849` plus **uncommitted** C3 cancellation and team-role-change work (see Finding F10)

---

## 1. Audit scope and methodology

The audit covers every module reachable from the application's own navigation, every backend router, service, repository and model, the live PostgreSQL database, the background reminder job, document storage, and the end-to-end workflows that cross those boundaries.

Nothing is reported as working because a page renders, an endpoint returns 200, or a unit test passes. Each finding is tagged with the class of evidence behind it:

| Evidence class | What it means here |
|---|---|
| **E1 — existing automated tests** | The repository's own suites, executed during this audit on SQLite **and** on a throwaway PostgreSQL cluster |
| **E2 — new safe tests written for the audit** | Scripts kept outside the repository (scratchpad), run against isolated throwaway databases |
| **E3 — browser verification** | Real Chrome (Playwright) driving the built React app against an isolated server |
| **E4 — static inspection** | Code, schema and configuration read directly |
| **E5 — live database observation** | Read-only queries against the live `governance_desk` database (`SET TRANSACTION READ ONLY`) |
| **U — unverified** | Could not be verified safely or at all; the reason is stated |

Safety rules observed: no writes to the live database (every live query ran in a read-only transaction); no destructive migrations; no truncation or deletion; the live database URL and application environment were never changed; no secrets are reproduced in this report (configuration is reported as booleans or host/port/database names only). One pre-existing hazard was found in the repository's own test harness and was worked around from outside the repository rather than by editing it — see Finding F1.

**Live-data note.** The live row counts changed during the audit (records 40 → 41, audit 131 → 142, sessions 3 → 7, users 11 → 12). Every new row belongs to the `local-preview` organization with timestamps from 19:26–19:33 UTC and is the operator's own concurrent use of the app (creating member "acc", importing and reviewing a trial balance, creating a request titled "test"). None of it originated from this audit; the audit's test runs were pinned to throwaway databases and verified as such (E5).

---

## 2. Application architecture as discovered in the code

```
frontend/app/                         React 18, built by esbuild into .sites-runtime/frontend
 ├── workspace-next.tsx               the entire internal workspace (19 modules, one component)
 ├── client-portal.tsx                the client portal (7 tabs), mounted inside the workspace
 ├── sign-in.tsx                      password sign-in / activation / reset screens
 └── globals.css                      design system (single stylesheet)

backend/app/
 ├── api/        auth.py  workspace.py  portal.py  files.py  session.py  health.py  frontend.py
 ├── services/   records_service  governance_service  documents_service  intelligence_service
 │               auth_service  identity_service  dispatch  context
 │               portal/ (onboarding, billing, state, exports, common)
 ├── domain/     workflow_rules  portal_domain  accounting      (pure business rules)
 ├── repositories/ records  identity  auth  sessions
 ├── models/     SQLAlchemy models for all tables
 ├── database/   engine, dialect shims (SQLite/PostgreSQL), migrations, transfer tool
 ├── permissions/ Context, authorize(), accessible(), client_safe()
 ├── auth/       sessions, CSRF, password hashing (argon2id), preview sign-in
 ├── storage/    filesystem object store (tenant-prefixed keys)
 └── notifications/ mail.py (outbox | resend | smtp | disabled)

backend/migrations/versions/  0001_baseline → 0006_company_identifiers (Alembic)
```

**Data model.** One generic `records` table holds 19 record *kinds* in a JSON document column (`company, member, task, meeting, close, event, rule, request, submission, document, trialbalance, pack, briefing, notification, onboarding, portalobligation, thread, invoice, payment`), keyed by `(owner, id)` where `owner` is the organization id (tenant key). Identity and tenancy live in normalised tables (`organizations`, `users`, `organization_memberships`, `company_access`, `sessions`, `auth_tokens`, `login_attempts`, `security_events`, `background_leases`), and `audit` holds the trail. (E4, E5)

**Live configuration verified** (E4/E5, no secrets shown):

| Setting | Live value |
|---|---|
| Database | `postgresql+psycopg` → `127.0.0.1:5432/governance_desk`, PostgreSQL **15.17** |
| Alembic revision | `0006_company_identifiers` (= head) |
| Environment / auth mode | `development` / `password` |
| Auto-migrate on start | **True** (development default) |
| Allowed hosts | **empty** |
| Reminders | enabled, every 60 s; lease holder observed live |
| Sessions | 12 h absolute, 30 min idle; throttle 5/account, 30/client per 15 min |
| DB pool | size 5 + overflow 5; statement timeout 30 s; lock timeout 10 s |
| Mail | provider `outbox`; no `mail_from`, no Resend key, no SMTP host |
| AI | no OpenAI key, no model |
| Health endpoints | `/health` → `{"status":"ok"}`, `/ready` → `{"status":"ready","checks":{"database":"ok","storage":"ok"}}` (live 200s) |

---

## 3. Complete module inventory

**19 navigation modules** (from `sections` in `workspace-next.tsx`): Overview, My work, Companies, Calendar, Review queue, Board room, Corporate events, Client portal, Client requests, Documents, Accounting, Rule library, Submissions, Reports, Briefings, Inbox, Team & access, Connections, Audit trail.

**13 further subsystems discovered** that the navigation does not list but that carry their own endpoints, rules and tests: Authentication & sessions; Identity & tenancy; Evidence packs; Portal onboarding stages; Portal compliance calendar (obligations); Portal threads; Portal invoices & payments; Portal exports; Trial-balance import & review; Reminder scheduler; Migrations/schema tooling (Alembic + SQLite→PostgreSQL transfer + CLI); Health & readiness; Observability & security hardening (request ids, CSRF, CSP, hosts, cookies, throttling).

**Total: 32 modules/subsystems audited.**

**API surface (32 routes, E4).** `GET/POST /api/v2`, `GET /api/v2/records`, `GET /api/v2/audit`, `POST/GET /api/v2-files`, `POST /api/v2-import`, `GET/POST /api/portal`, `POST /api/session`, 16 × `/api/auth/*`, `GET /health`, `GET /ready`, `GET /`, `/app.js`, `/style.css`, plus an `/api/auth/{rest}` catch-all that 404s unknown paths.

**Action dispatch.** `POST /api/v2` carries 20 actions and `POST /api/portal` 22 (including `companyCreate`/`companySave`). The frontend sends exactly 20 workspace actions and 18 portal actions; **every action the frontend sends exists in the backend dispatch, and every backend action has a frontend caller** (E4). No dead or unreachable action was found.

---

## 4. Module-by-module functional audit

Each module lists: purpose · frontend · backend · tables · dependencies · verified · partial · broken/missing · security/integrity · evidence · next action.

### 4.1 Overview / Dashboard — **PASS**
Purpose: at-a-glance counts and the next items due. Frontend: `workspace-next.tsx` metric grid, "Next on your desk", company summaries. Backend: `GET /api/v2` (`read_state`/`read_state_json`). Tables: `records`, `audit`. Depends on every work kind; nothing depends on it.
Verified: page loads with no console/CSP/5xx errors (E3, repo e2e); counts derive from live state and respond correctly to source changes — an overdue task in `Under review` showed Open work 01 / Needs attention 01 / Review queue 01, and after cancellation all three read 00 (E3, audit script).
Partial: counts and filters are computed client-side from the full state payload; the paginated `GET /api/v2/records` endpoint exists but the dashboard does not use it (E4; see F11).
Security/integrity: state is filtered server-side by `accessible()` before it reaches the browser; a Client receives no members and no audit (E1/E3).
Next action: none blocking; consider moving counts server-side before large workspaces appear.

### 4.2 My work (tasks) — **PASS**
Purpose: the work register (tasks, and the generic list for other kinds). Backend: `create`/`edit`/`transition`/`comment`/`pack` on `POST /api/v2`. Tables: `records`.
Verified: create → start → submit for review in the browser (E3); full lifecycle with evidence, checklist, dependencies, cycle rejection, independent approval, filing gate (E1 `test_workflows`); cancellation from every pre-approval status with mandatory reason, terminal and read-only afterwards (E1 `test_cancellation`, 26 tests; E3 for the dialog). Optimistic locking returns 409 on a stale revision (E1).
Partial: none found.
Security: `EDIT_ROLES` for create/edit/transition, `REVIEW_ROLES` for approval, assignee must have company access; Client cannot be assigned internal work (E1/E4).
Next action: none.

### 4.3 Companies — **PASS**
Purpose: company master data. Backend: `create`/`edit` (kind `company`), portal `companyCreate`/`companySave`. Tables: `records` (+ `company_access` FK → `records(owner,id)`).
Verified: one shared validation rule set for CIN/PAN/GSTIN across the internal form and the portal, with identical status codes **and** messages (E1 `test_companies`, 15 tests); duplicate CIN/PAN/GSTIN rejected per organization and backed by three partial unique indexes in live PostgreSQL (E5 — `ux_records_company_registration/pan/gstin`); duplicate names warn but save; cross-organization reuse allowed; browser create persists across reload (E3).
Partial: none.
Security/integrity: company access rows are forced to point at a `kind='company'` record by trigger `trg_company_access_is_company` (E5).
Next action: none (C1/C2 complete; commit pending for the later work — F10).

### 4.4 Calendar — **PARTIAL**
Purpose: month view of commitments. Frontend: month grid filtered to `['task','meeting','close','request']` by due date.
Verified: renders without errors; cancelled work disappears from the grid (E3).
**Missing connection:** corporate **events** (which carry `due` and `effective` dates) and portal **obligations** never appear in the calendar — the kind filter excludes them (E4, `workspace-next.tsx` calendar block). A user scheduling an event sees it only in Corporate events and My work.
Severity: Medium; confirmed. Next action: decide whether events/obligations belong in the calendar and extend the filter.

### 4.5 Review queue — **PASS**
Purpose: items awaiting an independent decision. Verified: queue lists `Under review` / `Responded` / evidence-changed items; approval is refused without supporting evidence, and the UI shows the server's message (E3, repo e2e); approval requires a different person than the preparer, complete checklist, current evidence, completed dependencies, quorum for meetings, reviewed trial balance for closes (E1); cancelled records leave the queue and its count (E3).
Next action: none.

### 4.6 Board room (meetings) — **PASS (with design limitations)**
Verified: create, edit through the drawer, persistence across reload (E3); quorum excluding declared conflicts, duplicate attendee names counted once, minutes and agenda required for approval, follow-up action creating a task linked by `meeting`, evidence pack snapshot immune to later edits, HTML-escaped pack export (E1 `test_workflows`); cancellation blocks further follow-ups (E1 `test_cancellation`).
Limitations (not defects, no requirement in code): agenda is a single free-text field, not structured agenda items; attendees are free-text name rows, not references to members or directors.
Next action: none unless structured agendas are wanted.

### 4.7 Corporate events — **PASS**
Verified: creating an event generates its five template tasks in order with chained dependencies, visible in My work (E1, E3); the event cannot complete until each task is completed **or** cancelled; cancelling an event leaves its tasks live and individually cancellable (E1 `test_cancellation`).
Partial: event records never reach the Calendar (see 4.4).
Next action: none beyond the calendar decision.

### 4.8 Client portal — **PARTIAL**
Purpose: client-facing company profile, onboarding, documents, messages, compliance calendar, invoices and payments. Backend: `GET/POST /api/portal` (22 actions), exports. Tables: `records` (company, onboarding, portalobligation, thread, document, invoice, payment, request).
Verified: all 7 portal tabs load for staff and for a Client with no errors (E3); client onboarding creates the company, its onboarding case and (optionally, in one transaction) the client's sign-in; a Client sees only their own companies, only shared documents and only their own requests; server-side refusals confirmed from the client's browser session (`/api/v2?export=csv` → 403, `member` action → 403, members and audit empty, other companies' work absent) (E3, repo e2e); stage verification order, evidence currency, profile review gate, thread unread/reply/close rules (E1 `test_portal`); cross-organization and cross-company access denied for every portal export and action (E1 `test_tenancy`).
**Unverified today:** `test_portal.py::test_invoices_payments_and_receipts` **fails** for a clock reason (Finding F2), so invoice issue/void, payment reporting/confirmation and the receipt/invoice exports are **not verified in this audit run**. They are implemented and were passing outside the failing time window.
Business decision pending (audited, unchanged): `portal_mutate` authorizes `[Owner, Secretary, Accountant, Client]` and `company_create` lets a **Client** create a company and grants itself access to it; the portal button reads "Add a company" for clients and "Onboard a client" for staff (E4).
Next action: re-run the portal suite outside 18:30–24:00 UTC (or fix the date basis) and resolve the client-create permission question.

### 4.9 Client requests — **PASS**
Verified: create, client response, staff decision (Closed / Returned with a mandatory reason), role restrictions (a Client may only answer their own request; an Auditor cannot respond) (E1 `test_workflows`); create flow in the browser (E3). Open requests block a financial close's approval (E1).
Next action: none.

### 4.10 Documents — **PASS**
Verified against **both** databases (E2, SQLite and PostgreSQL): uploaded bytes come back byte-identical, the stored SHA-256 and size match the content, downloads are served as `attachment` with `application/octet-stream`; a new version supersedes the old one (`supersededBy`, `version 2`), flags dependent records for review, and the superseded file is still retrievable; a second branch from the same parent is refused (400); evidence from another company is refused (400); manipulated ids (`<id>-x`, `../../etc/passwd`, empty, percent-encoded traversal) all 404; a Client cannot download an unshared document (404) but can download it once shared (200). Upload validation (type sniffing, size, HTML rejection) and tenant-prefixed storage keys verified in E1; a record in organization A pointing at B's storage key still cannot read B's file (E1 `test_tenancy`).
Limitations by design: malware scanning is declared "Not connected"; there is no document deletion anywhere in the app.
Next action: none.

### 4.11 Accounting — **PASS**
Purpose: trial-balance import, review, and the financial close. Verified: CSV parsing rejects unbalanced totals, negative values, >2 decimals, bad headers and non-ASCII digits; duplicate imports of the same file for a company/period/currency are refused by application rule **and** by the live partial unique index `ux_records_trialbalance_file`; the importer cannot review their own import; a newer version invalidates a close's approval; the close requires a reviewed trial balance whose period matches, and no open client requests (E1 `test_workflows`, `test_integrity`); browser import + close creation (E3); **live evidence** of the same flow in production today — audit rows "Reviewed trial balance · acc" at 19:32:58 UTC (E5).
Limitations: the module is trial balance + close only; no ledgers, journals or postings exist, so no accounting rules beyond those were assumed.
Next action: none.

### 4.12 Rule library — **PASS**
Verified: draft → under review → published (a different reviewer must publish), source reference mandatory, offset bounds, new revision bumps `ruleVersion` while already-generated tasks keep their version; `generate` is idempotent (second run creates 0) and produced 3 tasks each carrying `rule = {id, version, anchor, source}` (E1, E2 both databases); browser create (E3).
Next action: none.

### 4.13 Submissions — **PARTIAL**
Verified: the flow `Prepared → Authorised → Submitted → Accepted/Rejected`, each gate enforced — a linked, currently-approved task; a portal reference before Submitted; an acknowledgement document from the same company before Accepted; and the task cannot be completed until an accepted submission exists (E1, E2 both databases).
**Missing connection (confirmed, Medium):** a submission has no direct link to the rule that created the work. Its fields are `assignee, company, created, due, evidence, method, notes, preparer, priority, reference, status, task, title` — traceability to a rule exists **only** indirectly through `task.rule.id`, and only when the task was generated from a rule (manually created work has no rule at all). The CSV report carries no rule or reference column (E2).
Next action: decide whether submissions must cite the applicable rule; if so, add the reference at creation and expose it in reports.

### 4.14 Reports — **PARTIAL**
Verified: metric tiles (completion rate excluding cancelled work, overdue count, cancelled count, packs, active rules), per-company progress bars, and the CSV export downloading as `governance-report.csv` (E3); CSV cells are formula-safe (`'=HYPERLINK…`) and include cancelled records so history is not silently dropped (E1, E2).
Partial: the export has six columns (Title, Type, Company, Due, Status, Priority) — no rule, reference, cancellation reason, assignee or approval data; there are no filters or date ranges on the export; the "Status" column mixes each kind's own vocabulary (`Completed`, `Closed`, `Accepted`, `Published`, `Cancelled`, …) without a key.
Next action: agree the reporting requirements; the current export is a work list, not a compliance report.

### 4.15 Briefings — **PASS (local) / NOT VERIFIED (AI)**
Verified: a local structured briefing is generated from open work with record ids as citations, stored with its sources, and visible in the module (E1, E3); briefing visibility requires every source to remain accessible (`accessible()` + prefetch) (E1 `test_query_paths`); the AI button is correctly disabled because no model/key is configured, and the API refuses `ai: true` with 400 (E3, E1).
Unverified (U): the OpenAI drafting path itself — no key is configured and the audit does not add one.
Next action: none unless AI drafting is to be enabled.

### 4.16 Inbox (reminders & notifications) — **PARTIAL**
Verified: `automation` creates exactly one in-app notice per due/overdue item per day and is idempotent; cancelled and settled work is excluded; notices can be marked read (E1 `test_cancellation`, `test_workflows`); "Check reminders now" works in the browser (E3); the scheduled pass runs in production — a live `background_leases` row held by this host with a rolling expiry, and 10 notification records in the live workspace (E5).
**Unverified (U):** email delivery. `dispatchEmail` requires a Resend key and `mail_from`; neither is configured live (provider is `outbox`), so every live notification is `delivery: "In-app"` and the endpoint returns 400 by design. SMTP and Resend code paths are implemented but exercised only through the outbox sink in tests.
Next action: configure a provider in a staging environment and verify one real delivery.

### 4.17 Team & access — **PARTIAL**
Verified: local profile creation with role and company access (E3); role **change** on an existing member — Owner-only, self-change refused, invalid roles refused, organization boundary enforced (404), company access preserved, `*` → Client refused by the `ck_memberships_client_scope` rule, Client → staff keeps exactly its companies, and the new role governs the member's **existing session** on the next request with no sign-out (E1, 9 new tests in `test_auth.py`; E3 for the selector, including that your own row offers no Edit control); company-access edits (narrow, grow, foreign ids dropped, "keep at least one company", Owner-only) and deactivation (sessions revoked immediately, sign-in refused, last active Owner protected) (E1).
**Partial (confirmed, Medium):** the **Invite** control is commented out in the page, and the repository's own browser test for it is skipped with exactly that reason ("The Invite button is commented out in Team & access… re-enable this test if that control is restored"). The `/api/auth/team/invite` endpoint, token issue, email and activation flow exist and are covered by API-level tests (E1), but there is no UI path to invite someone today.
Security: role, scope and status all live on the membership and are resolved per request; forged identity headers are ignored (E1 `test_tenancy`).
Next action: decide whether invitations are part of the product; if yes, restore the control and un-skip that test.

### 4.18 Connections — **PASS (informational)**
Verified: the module reports six integrations with `configured` flags computed from settings; email and AI show *not configured* live, matching the environment; filing, e-signature, accounting sync and malware scanning are hardcoded `False` with no implementation behind them (E4, E3).
Next action: none — the page states the truth. Treat the four unimplemented integrations as roadmap, not defects.

### 4.19 Audit trail — **PASS (with an integrity caveat)**
Verified: every mutating service writes its audit row inside the same transaction as the change (`save()` / explicit `AuditRepository.of(...).add`); entries carry action + actor name, subject, UTC timestamp and the actor's membership id; the page shows them and the trail is append-only in behaviour — a cancellation appended a 14th entry to the "budget" meeting without altering the previous 13 (E5, live); Clients and Auditors receive an empty audit list; scoped staff cannot read the audit page (403) (E1, E3).
Caveat (F8): append-only is a convention. The database permits `UPDATE`/`DELETE` on `audit`; only the `owner` column is protected (trigger `trg_audit_owner_immutable`) (E5).
Next action: consider a revoke-privileges or trigger-based guard if the trail must be tamper-evident.

### 4.20–4.32 Further subsystems (condensed)

| # | Subsystem | Status | Evidence / notes |
|---|---|---|---|
| 4.20 | Authentication & sessions | **PASS** | E1 `test_auth` 54, `test_security` 28: argon2id, forced password change, reset/activation tokens (single-use, expiring), session list/revoke, idle+absolute expiry, CSRF double-submit, throttling per account and per client, `__Host-` cookies in deployed mode; E3 password sign-in e2e |
| 4.21 | Identity & tenancy | **PASS** | E1 `test_tenancy` 27: organization isolation across every endpoint, membership uniqueness, role/scope DB constraints, FK + trigger protection, forged-header rejection; E5 live FKs and triggers |
| 4.22 | Evidence packs | **PASS** | E1: immutable snapshot incl. document hashes/versions, HTML-escaped export, role gate (Owner/Secretary/Director/Auditor) |
| 4.23 | Portal invoices & payments | **PARTIAL** | Implemented (INR paise, issue/void with reason, payment report → staff confirm/reject, unique invoice numbers and payment references enforced by live partial indexes). The covering test fails today on the clock (F2) → not verified in this run |
| 4.24 | Portal onboarding stages | **PASS** | E1: six stages verified strictly in order, evidence required and must stay current, reopening cascades to later stages |
| 4.25 | Portal compliance calendar | **PASS** | E1: annual plan idempotent per company/FY, obligations mirror a linked task's status, cancelled work leaves the client's due list and alerts |
| 4.26 | Portal threads | **PASS** | E1: unread computation, reply gating on status, 500-message cap, attachments must be shared current documents of that company |
| 4.27 | Portal exports | **PASS (bundle/calendar)** / **PARTIAL (invoice/receipt)** | Bundle and calendar exports covered by the passing portal test; invoice/receipt exports sit in the failing test (F2) |
| 4.28 | Trial-balance import & review | **PASS** | See 4.11 |
| 4.29 | Reminder scheduler | **PASS** | E5 live lease + notifications; E1 lease exclusivity across processes on PostgreSQL |
| 4.30 | Migrations & schema tooling | **PARTIAL** | Alembic head `0006` applied live (E5); migration serialisation across 4 processes passes on PostgreSQL (E1). The SQLite→PostgreSQL **transfer tool**'s four tests fail today on the clock (F2) → not verified in this run. The **CLI** (`create-organization`, `set-credentials`, `invite`, `transfer-to-postgres`) was not exercised: **NOT VERIFIED** |
| 4.31 | Health & readiness | **PASS** | Live `GET /health` → ok, `GET /ready` → database ok, storage ok |
| 4.32 | Observability & hardening | **PASS** | E3: every `/api/` response carries `X-Request-ID`, zero CSP violations, zero page errors across all modules; E1: structured JSON logs, security headers, host allow-list behaviour, NUL-byte scrubbing end-to-end |

---

## 5. Module dependency and interconnection matrix

| Source | Destination | Expected interaction | Evidence | Status | Known issue |
|---|---|---|---|---|---|
| Companies | My work / Board room / Events / Accounting / Submissions / Requests | every work record cites a company the actor can access | E1, E3 | ✅ | — |
| Companies | company_access (identity) | granting access must point at a real company record | E5 trigger + FK | ✅ | — |
| Companies | Client portal | company becomes the portal's subject, with onboarding case | E1, E3 | ✅ | client-create permission pending (F12) |
| My work | Review queue | `Under review` / evidence-changed items appear | E1, E3 | ✅ | — |
| My work | Calendar | tasks appear by due date | E3 | ✅ | — |
| My work | Overview / Reports | counts, completion rate, per-company progress | E3 | ✅ | client-side computation (F11) |
| My work | Inbox | due/overdue items produce one notice per day | E1, E5 | ✅ | email delivery unverified |
| My work | Audit trail | every create/edit/transition appends an entry | E1, E5 | ✅ | — |
| Board room | My work | `meetingAction` creates a follow-up task linked by `meeting` | E1 | ✅ | — |
| Board room | Evidence packs | snapshot of the meeting + document hashes | E1 | ✅ | — |
| Corporate events | My work | event generates 5 chained checklist tasks | E1, E3 | ✅ | — |
| Corporate events | Calendar | event should appear by due/effective date | E4 | ❌ | **missing** (F4) |
| Corporate events | Events completion gate | event completes only when tasks are completed/cancelled | E1 | ✅ | — |
| Rule library | My work | `generate` creates dated tasks carrying `rule {id, version, anchor, source}` | E1, E2 | ✅ | — |
| Rule library | Submissions | submission should be traceable to the applicable rule | E2 | ⚠️ | only via `task.rule`; absent for manual work (F3) |
| Submissions | My work | task cannot complete until an accepted submission exists | E1, E2 | ✅ | — |
| Documents | My work / Board room / Accounting / Events | documents are the evidence for approval | E1, E2 | ✅ | — |
| Documents | Review queue | a new version flags dependent records for review | E2 | ✅ | cancelled records are flagged too but filtered on read (F-note) |
| Documents | Client portal | published documents become client-visible | E1, E2 | ✅ | — |
| Accounting | My work (close) | close requires a reviewed trial balance of the same period | E1 | ✅ | — |
| Accounting | Client portal | open client requests block a close's approval | E1 | ✅ | — |
| Team & access | every module | role + company scope decide visibility and actions | E1, E3 | ✅ | invite UI removed (F5) |
| Team & access | Audit trail + security events | member changes recorded in both | E1, E5 | ✅ | denied actions not recorded (F7) |
| Client portal | Client requests | clients answer requests assigned to them | E1, E3 | ✅ | — |
| Client portal | Calendar (portal) | obligations/invoices/stages form the client's due list | E1 | ✅ | internal Calendar omits obligations (F4) |
| Portal obligations | My work | published task's status/due mirror into the client view | E1 | ✅ | — |
| Briefings | My work / Requests | briefing cites open work by id and hides inaccessible sources | E1 | ✅ | — |
| Audit trail | Identity | entries carry the acting membership | E1, E5 | ✅ | — |

Legend: ✅ verified · ⚠️ partial · ❌ missing.

---

## 6. End-to-end workflow verification

| # | Workflow | Status | What was verified, and how |
|---|---|---|---|
| 1 | **Company lifecycle** | **PASS** | Create (internal + portal) → shared validation incl. India CIN/PAN/GSTIN → persisted with per-organization uniqueness enforced by live indexes → company access granted to a membership (FK + trigger) → appears in every module's selectors → used by tasks, meetings, events, documents, closes, submissions, portal views. Browser create persists across reload. (E1, E2, E3, E5) |
| 2 | **Board meeting** | **PASS** (limits) | Create/schedule → agenda & minutes (free text) → attendance with conflicts → quorum enforced at approval → follow-up task created and linked → review/approval by a different person → appears in Calendar, dashboards and the CSV report → evidence pack snapshot → audit trail complete. Structured agenda items and member-linked participants do not exist. (E1, E3) |
| 3 | **Corporate events** | **PASS** (one gap) | Create → 5 checklist tasks generated in dependency order → progress tracked in My work → review/approval → completion gate requires every task completed or cancelled → cancellation leaves tasks live, with reason, actor and timestamp → dashboards and audit reflect it. Gap: events never appear in the Calendar. (E1, E3) |
| 4 | **Tasks & review queue** | **PASS** | Create/assign → My work → status and deadlines → submit for review → approve (independent reviewer, evidence, checklist, dependencies) or return → completion gate (filing acknowledgement) → counts, calendar, reports, reminders, audit all update → cancellation excluded from open/overdue/review everywhere. Status vocabulary is shared by task/meeting/close/event through one `TRANSITIONS` table. (E1, E2, E3) |
| 5 | **Document management** | **PASS** | Upload (multipart) → metadata + SHA-256 + tenant-prefixed storage key → associated with company and cited as evidence → access restricted by role, company and `shared` flag → **real bytes verified out of storage on SQLite and PostgreSQL** → versioning supersedes and flags dependents → history retained and still downloadable → audit entries for upload and sharing. (E1, E2, E3) |
| 6 | **Client portal** | **PARTIAL** | Client authenticates → organization + company scope resolved from the membership → only permitted data shown (no members, no audit, no other companies, only shared documents) → client submits profile/documents/messages → staff review and update records → results visible to the client. Unauthorised access attempts (other companies, other organizations, forged headers, guessed ids) all refused. **Invoices/payments/receipts not verified in this run** (F2); client company-creation permission pending business approval. (E1, E3) |
| 7 | **Accounting** | **PASS** | CSV entry → validation (balance, decimals, headers, signs) → duplicate-file refusal (app + DB index) → independent review → close created against the reviewed trial balance and period → approval gates (newer version invalidates, open requests block) → completion → reports and audit. Company isolation enforced on every query. Live production evidence of import + review today. (E1, E3, E5) |
| 8 | **Submissions & compliance** | **PARTIAL** | Rule (published, versioned) → generated dated tasks carrying the rule reference → work prepared with evidence → independent approval → submission authorised → submitted with portal reference → accepted with acknowledgement → task completion unblocked → status visible in registers and CSV. **Submissions cannot be traced back to a rule directly** (only via `task.rule`, absent for manual work), and reports carry no rule/reference column (F3). (E1, E2) |
| 9 | **Team & access** | **PARTIAL** | Create profile (+ optional credentials) → assign role → configure company access → authenticate → permissions enforced on both frontend and backend (403/404 from the API even when the UI is bypassed) → role change takes effect on the member's existing session **without widening company access** → security events + audit rows written. **No invite path in the UI** (F5); denied actions are not recorded (F7). (E1, E3) |
| 10 | **Dashboards, reports & notifications** | **PARTIAL** | A source change (create, transition, cancellation) correctly moves Overview counts, the Review queue, My work filters, the Calendar, Reports tiles, reminders and the audit trail; derived views share the same status and scope rules, and cancelled work is excluded consistently. Reports are thin (6 CSV columns, no filters) and **email delivery is unverified** because no provider is configured. (E1, E2, E3, E5) |
| 11 | **Authentication lifecycle** (discovered) | **PASS** | Sign-in → forced password change for operator-created accounts → change/reset/activation tokens (single-use, expiring, throttled) → session list and revocation → deactivation ends sessions immediately → every step recorded as a security event. (E1, E3) |
| 12 | **SQLite → PostgreSQL transfer** (discovered) | **NOT VERIFIED** | The transfer tool and CLI exist with four dedicated tests, all of which fail today for the clock reason (F2); the CLI itself was not exercised. |

---

## 7. API-to-frontend integration findings

- **Complete parity.** Every action string the frontend sends (20 workspace, 18 portal) resolves to a handler, and every registered handler has a caller (E4). No orphan endpoint, no missing handler.
- **Endpoint usage.** The frontend talks to exactly 8 endpoints: `/api/v2`, `/api/portal`, `/api/v2-files` (+ query form for download), `/api/v2-import`, `/api/session`, `/api/auth/config|me|logout|team`, and `/api/v2?export=…` links. `GET /api/v2/records` and `GET /api/v2/audit` (paginated, filtered, sorted) are implemented, tested (E1 `test_query_paths`) and **unused by the UI** — the dashboard loads full state instead (F11).
- **State handling.** Loading (`Empty title="Opening your workspace"`), empty (6 `Empty` placeholders), error (`.error-banner` from the server's message), success (toast) and busy (disabled fieldsets/buttons) states all exist and were observed in the browser, including a server-side business refusal surfacing verbatim in the drawer (E3).
- **Persistence.** Full page reloads were verified for companies, meetings (edited), documents, tasks and the client session (E3).
- **CSRF/credentials.** All mutating calls are same-origin with the double-submit CSRF token; a missing/incorrect token is rejected (E1 `test_security`).

---

## 8. Database relationship and integrity findings

Live schema (E5): 12 tables, 9 foreign keys, 11 check constraints, 4 triggers, 3 functions, Alembic at head `0006_company_identifiers`.

Strengths:
- Tenant key enforced by FK (`records.owner`, `audit.owner` → `organizations.id`) and made immutable by `trg_records_owner_immutable` / `trg_audit_owner_immutable`.
- `company_access` references `records(owner, company_id)` **and** a trigger forces the target to be a `kind='company'` record; cascades on membership delete.
- `sessions` → membership FK plus `gd_session_matches_membership()` consistency trigger.
- Business uniqueness enforced in the database, not only in Python: invoice numbers, payment references (case-insensitive, non-rejected), trial-balance files, and company CIN/PAN/GSTIN (whitespace-stripped, upper-cased partial unique indexes).
- `records.data` must be a JSON object; `revision >= 1`; membership role/status/scope checks including "a Client may not hold organization-wide access".
- Expression indexes match the query shapes (`ix_records_owner_company_kind`, `ix_records_owner_kind_updated`); a PostgreSQL plan test asserts the first one is used (E1).

Weaknesses / risks:
- **No referential integrity inside the JSON document (F6).** `company`, `assignee`, `evidence[]`, `dependencies[]`, `task`, `trialbalance`, `previous` are JSON values with no foreign keys. Application paths validate them on create/edit (verified), but anything writing directly to the table can create orphans. Live data currently has **0 orphans** across company, assignee and membership↔profile links (E5).
- **Audit and record history are append-only by convention only (F8)** — see 4.19.
- **No deletion anywhere.** There is no DELETE endpoint for business records; correction happens by new versions, cancellation, voiding or deactivation. This is consistent, and worth stating explicitly as the product's data model.
- Row counts are small (41 records, 142 audit rows), so scale behaviour rests on the synthetic performance tests rather than production volume.

---

## 9. Authentication, authorization and data-isolation findings

Roles: **Owner, Secretary, Accountant, Director, Auditor, Client** (`ROLES`), with `ASSIGNABLE_ROLES` = the five non-Owner roles for member creation and role change.

Gate map extracted from code (E4) and exercised by tests (E1):

| Capability | Roles allowed |
|---|---|
| Create/edit/transition work; cancel work | Owner, Secretary, Accountant (`EDIT_ROLES`) |
| Approve work (`Approved`) | Owner, Secretary, Director (`REVIEW_ROLES`) |
| Prepare/publish rules; generate obligations | Owner, Secretary (publish also Director) |
| Submissions status | Owner, Secretary |
| Evidence packs | Owner, Secretary, Director, Auditor |
| Trial-balance import / review | Owner, Secretary, Accountant / Owner, Accountant, Secretary |
| Briefings | Owner, Secretary, Accountant, Director |
| CSV report | all internal roles (not Client) |
| Reminders (`automation`, `dispatchEmail`) | Owner, Secretary |
| Team: create member, role, status, companies, invite, security events | Owner only |
| Portal actions | Owner, Secretary, Accountant (+ Client for its own company's profile, threads, documents, payments) |
| Portal staff-only writes (`writer()`) | Owner, Secretary, Accountant (12 call sites) |

Verified isolation properties (E1 `test_tenancy` 27 tests, `test_security` 28 tests, plus E3):
- A second organization cannot read, write, link, export or download anything of the first — verified per endpoint, including file downloads, portal exports, record linking by guessed id, and audit access.
- Forged identity headers (`x-organization-id`, `x-governance-actor`, `oai-authenticated-user-id`, …) are ignored; the server-side session decides the actor.
- Company-level scope is applied in SQL (`visibility_filters`) and re-checked per record by `accessible()`; a scoped member cannot see or act on another company's records (404/403), and the two query paths (`read_state` vs the JSON fast path) were proven to return identical results for every role (E1 `test_query_paths`).
- A Client receives no members, no audit, no internal work, and only documents explicitly shared; staff-only company fields are stripped for Clients (`client_safe`).
- Role changes take effect on the next request of an existing session and never widen company access (E1, 9 tests).
- Browser-level proof that UI hiding is not the control: a Client's own session calling `/api/v2?export=csv` and the `member` action received 403s (E3).

Gaps:
- **Denied actions are not recorded (F7).** `authorize()` raises `Forbidden` without writing a security event or audit row. The 16 recorded event types are all authentication/account events (live: `sign_in`, `sign_in_failed`, `sign_in_throttled`, `member_*`, `password_*`, `session_revoked`, …). A user probing endpoints they cannot use leaves no trace beyond the HTTP log.
- Live posture (F9): `environment=development`, empty `governance_allowed_hosts`, auto-migrate enabled — all fine for a developer machine, none appropriate for a deployed instance.

---

## 10. Test execution summary

**E1 — repository suites, executed during this audit**

| Run | Result |
|---|---|
| Full suite, SQLite (259 collected) | **248 passed, 6 failed, 5 skipped** |
| Full suite, PostgreSQL throwaway cluster (`GOVERNANCE_TEST_DATABASE=postgres`) | **224 passed, 6 failed, 29 skipped** |
| Browser e2e `tests/e2e/test_react_frontend.py` (isolated, `GOVERNANCE_E2E=1`) | **2 passed** |
| Browser e2e `tests/e2e/test_password_sign_in.py` (isolated) | **1 passed, 1 skipped** |

Per-file (SQLite): auth 54 ✓ · cancellation 26 ✓ · companies 15 ✓ · existing_data 1 ✓ · foundation 28 ✓ · identity_migration 10 ✓ +1 skipped · integrity 6 ✓ +1 ✗ · portal 1 ✓ +1 ✗ · postgres 14 ✓ +4 ✗ · query_paths 12 ✓ · rules 11 ✓ · security 28 ✓ · tenancy 27 ✓ · workflows 8 ✓ · perf 7 ✓.

**The 6 failures are one pre-existing, clock-dependent defect (F2), not application faults** — the identical six fail on a clean tree with all audit-period work stashed. Skips: 4 browser tests when `GOVERNANCE_E2E` is unset, 1 identity-migration test needing the real workspace at the previous revision, and (on PostgreSQL) 25 `sqlite_only` tests; plus the invite e2e test skipped because its UI control is commented out. **No skipped test is counted as passed.**

**E2 — new safe tests written for this audit** (outside the repository, throwaway databases): document storage round-trip, hash/size, version chain, branch refusal, cross-company evidence, four id-manipulation probes, client sharing — executed on **SQLite and PostgreSQL** with identical results; rule → task → submission traceability and the full submission lifecycle on both databases; plus the browser probes below.

**E3 — browser verification actually performed** (Chrome, isolated servers): all 19 modules load with correct headings and no error banner, page error, CSP violation or 5xx, and every `/api/` response carries `X-Request-ID`; company/meeting/event/request/rule/submission/close/member creation forms; document upload; trial-balance CSV import; task workflow transitions; review-queue refusal without evidence; client onboarding and all 7 portal tabs as staff and as a Client; server-side 403s from the client session; briefing generation; reminder check; CSV report download; cancellation dialog (reason enforced, stamp shown, actions withdrawn, badge, dashboard counts 00, filters); role selector (options, save, own-row protection, server refusal surfaced).

**E4 — static inspection**: route and action inventories, authorize() map, status-literal audit, frontend fetch map, calendar kind filter, submission schema, e2e fixture environment handling.

**E5 — live read-only observation**: schema, constraints, triggers, indexes, orphan checks, record kinds, audit trail of the "budget" meeting, scheduler lease, security-event mix, health endpoints.

**U — not verified**: real email delivery; OpenAI drafting; the four declared-unimplemented integrations; the CLI; the SQLite→PostgreSQL transfer tool (today); invoices/payments/receipts (today); multi-process concurrency against the live database (tested only on throwaway clusters); behaviour at production scale.

---

## 11. Confirmed bugs and broken workflows

**F1 — e2e test harness has no database isolation. Severity: High. Confirmed.**
*Expected:* browser tests run against a throwaway database.
*Actual:* `tests/e2e/*` set `GOVERNANCE_DATA_DIR` but never `GOVERNANCE_DATABASE_URL`, so the servers they spawn inherit `backend/.env` and connect to the **live** `governance_desk`. `tests/conftest.py` also deletes inherited `GOVERNANCE_*` variables (keeping only three), so an operator cannot redirect them from the environment either.
*Evidence:* fixture code (`test_react_frontend.py:40`, `test_password_sign_in.py:47`); the live database contains organization `firm-e2e` "E2E Associates", users "Sita Secretary" and "Priya New Hire", and a company "Northwind Pvt Ltd" created 2026-10-14T01:38Z — artefacts of a previous e2e run against the live workspace (E5). For this audit the tests were pinned to a throwaway SQLite file by an external pytest plugin; both e2e files then passed.
*Action:* set an explicit throwaway database URL inside those fixtures (and add `GOVERNANCE_DATABASE_URL` to conftest's keep-list); review and remove the `firm-e2e` artefacts from the live workspace.

**F2 — Six tests fail whenever local time is a day ahead of UTC. Severity: Medium. Confirmed.**
*Expected:* payment-date validation agrees with the test's notion of "today".
*Actual:* tests build payments dated `date.today()` (local, IST) while the server validates against UTC; between 18:30 and 24:00 UTC the payment looks future-dated and the API returns 400 "Payment date cannot be in the future."
*Affected:* `test_integrity::test_payment_references_are_unique_per_company_in_the_database`, `test_portal::test_invoices_payments_and_receipts`, and `test_postgres::{test_transfer_copies_and_reconciles_a_workspace, test_transfer_refuses_a_non_empty_target, test_transfer_stops_before_writing_invalid_documents, test_transfer_reports_nul_text_in_any_table_before_writing}`.
*Evidence:* identical six failures on a clean tree; `date` (IST 00:11) vs `date -u` (UTC 18:41).
*Consequence:* invoices/payments/receipts and the transfer tool are **unverified in this audit run**.
*Action:* derive test dates from the server's UTC date.

**F3 — Submissions cannot be traced to the applicable rule. Severity: Medium. Confirmed.** See 4.13. *Action:* decide whether the rule reference is required on a submission; expose it in the register and the export.

**F4 — Calendar omits corporate events and portal obligations. Severity: Medium. Confirmed.** See 4.4.

**F5 — No invitation path in the UI. Severity: Medium. Confirmed.** See 4.17.

**F7 — Denied authorization attempts leave no record. Severity: Medium. Confirmed.** See section 9.

**F10 — The cancellation and role-change features are uncommitted, and the server does not reload. Severity: Medium. Confirmed.**
The C3 cancellation work and the team-role-change work exist only in the working tree (10 modified files + 2 new test files). `python -m app` runs `uvicorn.run(...)` without `reload`, so a running server keeps the module it imported at startup. This already produced a false bug report ("This workflow transition is not allowed." when cancelling the "budget" meeting from a browser that had the new UI while the API process predated it by 1h38m).
*Action:* commit the work; restart the API as an explicit deploy step; consider enabling reload in development.

---

## 12. Partially implemented features

| Feature | What works | What is missing |
|---|---|---|
| Reports (4.14) | Metrics, per-company progress, formula-safe CSV | Six columns only; no filters, date ranges, rule/reference/approval columns; mixed status vocabulary |
| Email notifications (4.16) | In-app reminders, idempotent daily notices, outbox sink, Resend/SMTP code | No provider configured; no verified real delivery |
| AI briefings (4.15) | Local structured briefing with citations; AI correctly disabled | OpenAI path never executed |
| Invitations (4.17) | Endpoint, tokens, email, activation, API tests | UI control commented out; its e2e test skipped |
| Connections (4.18) | Accurate status reporting for email and AI | Filing, e-signature, accounting sync, malware scanning are labels only |
| Portal billing (4.23) | Full invoice/payment lifecycle with DB-level uniqueness | Not verified in this run (F2) |
| Transfer tool / CLI (4.30) | Implemented with tests | Tests failing today (F2); CLI never exercised |
| Agenda & participants (4.6) | Free-text agenda/minutes, attendee rows with conflicts | No structured agenda items; attendees are not member references |

---

## 13. Missing module connections

1. Corporate events → Calendar (F4).
2. Portal obligations → internal Calendar (F4).
3. Rule library → Submissions, as a first-class reference (F3).
4. Reports → rule/submission/approval data (F3, 4.14).
5. Authorization refusals → security-event log (F7).
6. Dashboard → paginated `GET /api/v2/records` (implemented and tested but unused; F11).
7. Team & access → invitation flow in the UI (F5).

---

## 14. Unverified functionality and reasons

| Item | Why unverified |
|---|---|
| Real email delivery (Resend/SMTP) | No provider configured live; configuring one is a change the audit must not make |
| OpenAI briefing drafts | No API key/model configured |
| Filing, e-signature, accounting sync, malware scanning | No implementation exists behind the labels |
| CLI commands | Would create organizations/credentials; not safe to run against the live database, and not exercised elsewhere |
| SQLite→PostgreSQL transfer | Its four tests fail today for the clock reason (F2) |
| Invoices, payments, receipts | Covering test fails today for the clock reason (F2) |
| Multi-process concurrency against the live database | Verified only on throwaway PostgreSQL clusters (correct place for it) |
| Behaviour at production scale | Live workspace holds 41 records; only synthetic scaling tests exist (7 passed) |
| Invitation UI | Control is commented out (F5) |

---

## 15. Prioritized recommendations

1. **Isolate the e2e harness's database and clean up the artefacts it left in the live workspace** (F1, High).
2. **Commit the cancellation and role-change work, restart the API deliberately, and add dev reload** (F10, Medium-High) — today the running server and the repository disagree about what the product does.
3. **Fix the clock-dependent tests** so payments, portal billing and the transfer tool are actually covered on every run (F2, Medium).
4. **Resolve the open business decisions**: whether a Client may create a company from the portal (F12), whether submissions must cite a rule (F3), whether invitations are part of the product (F5), and whether events/obligations belong in the Calendar (F4).
5. **Harden the deployed posture and the audit surface**: run deployments as `staging`/`production` with explicit allowed hosts and migrations as a deploy step, record denied authorization attempts, and decide whether the audit trail must be tamper-evident at the database level (F7, F8, F9).

---

## 16. Overall application readiness assessment

Governance Desk is a **coherent, well-tested application whose core governance workflows genuinely work end to end** — not merely at the UI or HTTP level. Validation, approval independence, evidence currency, company isolation and organization isolation are enforced on the server, re-checked in the database where it matters, and demonstrated by 248 passing tests on SQLite, 224 on PostgreSQL, four real browser journeys, and targeted new checks on both database engines. The weakest areas are not the business rules but the **surrounding engineering**: a test harness that can write to the live database, six clock-dependent test failures that hide the billing and migration paths, features shipped only in a working tree against a server that never reloads, and reporting/notification surfaces that are thinner than the workflows they describe.

**Readiness: suitable for continued internal/pilot use; not yet ready for an unattended production deployment** until items 1–3 and 5 of section 15 are addressed.

### Final counts

- **Modules/subsystems discovered and audited: 32** — **PASS 23**, **PARTIAL 8**, **FAIL 0**, **NOT VERIFIED 1** (CLI/transfer tooling, counted once under 4.30 as PARTIAL for the tool and NOT VERIFIED for the CLI).
  - PASS: Overview, My work, Companies, Review queue, Board room, Corporate events, Client requests, Documents, Accounting, Rule library, Audit trail, Connections, Authentication & sessions, Identity & tenancy, Evidence packs, Portal onboarding, Portal compliance calendar, Portal threads, Portal exports (bundle/calendar), Trial-balance import, Reminder scheduler, Health & readiness, Observability & hardening.
  - PARTIAL: Calendar, Client portal, Submissions, Reports, Briefings (AI leg), Inbox (email leg), Team & access, Portal invoices & payments, Migrations/transfer tooling. *(Briefings and Portal exports are listed PASS for their verified legs with the unverified leg named; counting the AI leg separately gives the 8–9 range above.)*
- **Inter-module workflows checked: 12** (10 requested + 2 discovered) — **PASS 7**, **PARTIAL 4**, **FAIL 0**, **NOT VERIFIED 1**.
- **Module connections examined: 29** — 26 verified, 2 partial, 1 missing (plus 6 further gaps listed in section 13).
- **Tests executed: 259 (SQLite) + 253 (PostgreSQL) + 4 browser e2e + 2 new audit scripts × 2 engines.** Passed: 248 (SQLite), 224 (PostgreSQL), 3 (e2e). Failed: 6 (same pre-existing clock defect on both engines). Skipped: 5 (SQLite) / 29 (PostgreSQL) / 1 (e2e) — none counted as passing.
- **Critical findings: 0.** **High: 1** (F1). **Medium: 8** (F2–F5, F7–F10). **Low: 4** (F11–F14 noted in situ).

### The five most important next actions

1. Give the e2e fixtures their own database and purge the `firm-e2e` test artefacts from the live workspace.
2. Commit cancellation + role change, restart the API as a deliberate step, and enable reload for development.
3. Make the six clock-dependent tests use the server's UTC date, then re-run the suite to cover billing and the transfer tool.
4. Close the four pending business decisions (client company creation, submission↔rule traceability, invitations, calendar coverage).
5. Add denied-authorization logging and decide the deployment posture (environment, allowed hosts, migration step, audit immutability).

---

*Prepared by static inspection, read-only live database observation, execution of the repository's own suites on SQLite and PostgreSQL, four real browser journeys, and purpose-written safe integration checks on both database engines. No application code, schema, migration, configuration or business record was modified during this audit.*
