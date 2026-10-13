# Governance Desk

A company-secretarial workspace: board meetings, corporate events, compliance, documents,
accounting close, submissions, a client portal and INR billing.

```
React (source: frontend/ → build: .sites-runtime/frontend/)
  ↓  same-origin fetch: /api/v2, /api/v2-files, /api/v2-import, /api/portal, /api/session
FastAPI (backend/)
  ↓  routers → services → repositories
SQLAlchemy + Alembic
  ↓
SQLite (local development: .sites-runtime/preview/preview.sqlite)  or  PostgreSQL (staging/production)
  + local files (.sites-runtime/preview/files/)
```

By default it runs as a **local preview** on `127.0.0.1` with simulated team roles. Real sign-in
(`GOVERNANCE_AUTH_MODE=password`, [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md)) and PostgreSQL
(`GOVERNANCE_DATABASE_URL`, [docs/POSTGRESQL.md](docs/POSTGRESQL.md)) are available; staging and
production require both.

## Requirements

| Tool | Version | Used for |
|---|---|---|
| Python | 3.11 or newer (tested on 3.11, 3.12 and 3.14) | The API |
| Node.js | 22.13 or newer (Node 24 recommended; `nvm use` reads `.nvmrc`) | Building the React app only |
| pnpm | 11.19.0 (`corepack enable`) | Frontend dependencies |
| Google Chrome | any recent | Optional browser test only |

## First-time setup

From the project folder:

```sh
# 1. Frontend dependencies and build
nvm use
pnpm install --frozen-lockfile
pnpm frontend:build

# 2. Python virtual environment and API dependencies
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt     # or requirements.txt for runtime only
```

Configuration is optional in development. Copy `backend/.env.example` to `backend/.env` to change
settings; every option is documented there. The project-level `.env` is also read. Neither `.env` file
is tracked by git.

## Run the app

```sh
cd backend
source .venv/bin/activate
python -m app
```

Then open **http://127.0.0.1:5174/**. The client portal is at http://127.0.0.1:5174/#client-portal.

- Use `127.0.0.1` exactly; other host names are rejected (DNS-rebinding protection).
- `pnpm api` from the project folder does the same as `python -m app`.
- The equivalent uvicorn command, with auto-reload while you edit Python code:
  `uvicorn app.main:app --host 127.0.0.1 --port 5174 --reload`
  If you change the port, open the matching address. The API accepts the port it is bound to.

### React frontend

FastAPI serves the built React app; there is no separate frontend server.

```sh
pnpm frontend:build      # one-off build into .sites-runtime/frontend
pnpm frontend:watch      # rebuild app.js on every change (reload the browser)
pnpm typecheck           # TypeScript check of the frontend
```

### SQLite and files

- **Location:** data lives in `.sites-runtime/preview/`. This is the same folder the original Node backend used, so existing records and uploads carry over unchanged.
  - `preview.sqlite`: tables `records`, `audit` and `sessions`
  - `files/`: uploaded documents and trial balances
- **Setup:** none needed in development. On start the app applies database migrations (Alembic): a new
  database is created, and an existing one is adopted without changing any row (see "Database migrations").
- **Moving the data:** set `GOVERNANCE_DATA_DIR` to use a different folder, for example one outside the
  project. Staging and production require it, and it must be outside the source tree there.
- **Backup:** stop the API, then copy `preview.sqlite` and `files/` together.
- **Pre-migration copy:** a copy taken before the switch to FastAPI is at `.sites-runtime/preview/preview.before-fastapi.sqlite`.

### Environment profiles

`GOVERNANCE_ENVIRONMENT` selects the profile. The app checks its configuration before starting and
refuses to start if the configuration is unsafe, listing every problem without showing secret values.

| | development (default) | test | staging / production |
|---|---|---|---|
| Local-preview sign-in | yes | yes | **rejected** (there is no real sign-in until Phase 3, so every API call returns 401) |
| `GOVERNANCE_DATA_DIR` | optional (`.sites-runtime/preview`) | **required** | **required, outside the source tree** |
| `GOVERNANCE_ALLOWED_HOSTS` | optional | optional | **required** (public host names) |
| API docs (`/api/docs`, `/api/openapi.json`) | on | on | off (`GOVERNANCE_API_DOCS=true` to enable) |
| Database migrations on start | applied | applied | verified only; run `alembic upgrade head` when deploying |
| HSTS, `Secure` session cookie | off (plain HTTP) | off | on |

### Environment variables

All of them are listed with explanations in `backend/.env.example`. The main ones:

| Variable | Default | Purpose |
|---|---|---|
| `GOVERNANCE_ENVIRONMENT` | `development` | Profile: `development`, `test`, `staging`, `production` |
| `GOVERNANCE_DATABASE_URL` | empty (SQLite file in the data folder) | PostgreSQL URL (`postgresql+psycopg://...`); required in staging/production; secret. Pool settings: `GOVERNANCE_DB_*` |
| `GOVERNANCE_DATA_DIR` | `.sites-runtime/preview` (development) | SQLite database (when no URL is set) and `files/` folder |
| `GOVERNANCE_FRONTEND_DIR` | `.sites-runtime/frontend` | Built React app |
| `HOST` / `PORT` | `127.0.0.1` / `5174` | Address used by `python -m app` |
| `GOVERNANCE_ALLOWED_HOSTS` | empty | Extra accepted `Host` headers, comma separated |
| `GOVERNANCE_AUTH_MODE` | `local-preview` | `local-preview` (development/test only) or `password` (built-in sign-in) |
| `GOVERNANCE_WORKSPACE_ID` | `local-preview` | Development preview organization (created on first start in development/test) |
| `GOVERNANCE_SESSION_HOURS` | `12` | Session lifetime |
| `GOVERNANCE_API_DOCS`, `GOVERNANCE_AUTO_MIGRATE`, `GOVERNANCE_HSTS` | per profile | Override the profile default |
| `GOVERNANCE_CSP` | built-in policy | Full Content-Security-Policy override |
| `GOVERNANCE_LOG_LEVEL` / `GOVERNANCE_LOG_FORMAT` | `INFO` / `json` | Logging (`text` is easier to read locally) |
| `GOVERNANCE_REMINDERS_ENABLED` / `_INTERVAL_SECONDS` | `true` / `60` | In-app reminder check while the API runs |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | empty | Optional AI-drafted briefings (the key is masked in logs) |
| `RESEND_API_KEY`, `MAIL_FROM` | empty | Optional email dispatch of reminders (the key is masked in logs) |

### Database migrations

The schema is managed with Alembic (`backend/migrations/`):

- `0001_baseline` describes the existing `records`, `audit` and `sessions` tables and their indexes.
- `0002_identity_tenancy` adds organizations, users, memberships and company access (see
  "Organizations, users and access").
- `0003_authentication` adds credentials, session lifecycle, activation/reset tokens, sign-in
  throttling and security events (new columns and tables only).
- `0004_business_uniqueness` adds database-enforced uniqueness for issued invoice numbers, payment
  references and trial-balance files (it stops, changing nothing, if existing data has duplicates).

Every revision runs on SQLite and PostgreSQL ([docs/POSTGRESQL.md](docs/POSTGRESQL.md)).

```sh
cd backend
.venv/bin/alembic upgrade head                        # GOVERNANCE_DATABASE_URL, else the SQLite file in GOVERNANCE_DATA_DIR
.venv/bin/alembic -x db=/path/to/copy.sqlite upgrade head
.venv/bin/alembic -x url=postgresql+psycopg://user:pass@host/db upgrade head
.venv/bin/alembic current                             # shows 0004_business_uniqueness (head)
```

- **New database:** the tables and indexes are created.
- **Existing database** (from the Node backend or from FastAPI before Alembic): it is adopted. Its tables
  are checked for the expected columns and kept as they are; only missing indexes and Alembic's
  `alembic_version` table are added. No row is read, changed or deleted.
- **Upgrading to `0002`:** each existing tenant key becomes an organization with the same id, and
  each workspace profile becomes a user plus a membership with the same role, status and company
  scope. Record ids, file keys and timestamps don't change. The whole upgrade runs in one
  transaction, DDL included, so a failure leaves the database at `0001`. It stops, without changing
  anything, on a profile with an unknown role or a Client with access to every company. Company
  references to companies that don't exist are skipped and listed in the migration log; they granted
  no access before either.
- **Downgrading** is refused for 0001–0003, because it would drop data. Restore a backup instead.
- To try a migration on real data, use a copy (`-x db=...`), never the live file.

### Health checks

| Endpoint | Meaning | Response |
|---|---|---|
| `GET /health` | Liveness: the process is serving requests | `200 {"status":"ok"}` |
| `GET /ready` | Readiness: the database answers at the latest migration and document storage is writable | `200 {"status":"ready","checks":{...}}` or `503` with the failing check |

Neither response includes paths, versions or error details. Both accept any `Host` header, so a load
balancer can probe them.

### Logs, request IDs and security headers

- **Logs:** each log line is one JSON object on stdout (`ts`, `level`, `logger`, `message`, `request_id`).
  Every request writes one `http_request` line with the method, path (without the query string),
  status and `duration_ms`. Cookies, tokens, passwords and request bodies are never logged.
- **Request IDs:** every response carries `X-Request-ID`. A valid incoming `X-Request-ID` is kept, so
  IDs can be followed across a proxy.
- **Security headers on every response:** `X-Content-Type-Options`, `Referrer-Policy`,
  `X-Frame-Options: DENY`, `Content-Security-Policy`, `Cross-Origin-Opener-Policy`,
  `Cross-Origin-Resource-Policy` and `Permissions-Policy`, plus HSTS in staging and production.
- **CSP:** it allows only the app's own script, stylesheet and API calls. The browser tests fail on any
  CSP violation.

## Tests

```sh
cd backend && source .venv/bin/activate

python -m pytest                              # whole suite on SQLite, plus the PostgreSQL-specific tests
GOVERNANCE_TEST_DATABASE=postgres python -m pytest   # whole suite on PostgreSQL
GOVERNANCE_E2E=1 python -m pytest tests/e2e   # React app in Chrome against a throwaway FastAPI server
```

PostgreSQL tests use `GOVERNANCE_TEST_POSTGRES_URL` (an administrator URL; tests create and drop
their own databases) or, if it is unset, start a throwaway local cluster from the installed
PostgreSQL binaries. Without either they are skipped.

From the project folder:

```sh
pnpm test:compat   # the original Node-era API suites (tests/phases.mjs, tests/client-portal.mjs), unchanged, against FastAPI
pnpm test:api      # same as python -m pytest
```

The browser test needs `pnpm frontend:build` first. It uses the installed Google Chrome through Playwright, so no browser download is needed.

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request, using Python 3.12 and Node 24 with
pnpm 11.19.0:

1. Installs dependencies (`pnpm install --frozen-lockfile`, `pip install -r backend/requirements-dev.txt`).
2. Runs the frontend typecheck and build.
3. Applies the migrations to a fresh SQLite database, upgrades one from the first revision, and
   applies them to a fresh PostgreSQL database (a `postgres:16` service container).
4. Runs the identity, tenant-isolation and authentication tests, then the full backend suite on
   SQLite and again on PostgreSQL.
5. Runs the 325 compatibility checks on SQLite and on PostgreSQL, and the browser tests.

## Organizations, users and access

Each corporate-services firm is an **organization**. Its id is the tenant key stored on every record,
audit entry and file key, so one organization's data is never in another's queries.

| Table | Holds |
|---|---|
| `organizations` | id, name, status (`active`/`inactive`), created/updated timestamps |
| `users` | a person: display name, optional email (unique, case-insensitive), status. No passwords yet |
| `organization_memberships` | user ↔ organization with a role (`Owner`, `Secretary`, `Accountant`, `Director`, `Auditor`, `Client`), status, and either all companies or listed ones. One membership per user per organization |
| `company_access` | the companies a membership may see. Foreign keys tie it to a company of the **same** organization |

**How a request is authorized:**

1. The session cookie maps to a server-side session bound to one membership.
2. The organization, user and membership must all be active.
3. The role and company scope come from the membership tables.

Nothing the browser sends (headers, body fields, record ids) selects the organization. An id from
another organization simply isn't found (404). The workspace profile records (kind `member`) remain
the display profiles that the React app and business records use. Every code path that creates a
profile writes its membership in the same transaction, and authorization reads only the membership.

**Database guards** (SQLite triggers; on PostgreSQL foreign keys plus triggers):

- a record or audit entry must belong to an existing organization, and can never move to another one
- company access must point to a company record
- a session must match its membership

**Files** are only read through their organization's key prefix, so a record that points at another
organization's key still gets 404. **Audit entries** are scoped to the organization; new ones also
store the acting membership (older entries keep `NULL`). **Reminders** run for every active
organization, each with its own queries. A database lease (`background_leases`) makes sure only one
application process runs each pass.

**Development preview:**

- Opening the app signs in as the Owner of the preview organization (`GOVERNANCE_WORKSPACE_ID`).
- "Preview as" switches to another active membership of the **same** organization.
- Both are refused in staging and production.
- Real sign-in arrives in Phase 3, through the same sessions.

### Role permissions (enforced on the server; unchanged by Phase 2)

| Capability | Owner | Secretary | Accountant | Director | Auditor | Client |
|---|---|---|---|---|---|---|
| Sees companies | all | per access | per access | per access | per access | listed only; no internal work |
| Create and edit work (tasks, meetings, closes, events, requests, submissions, companies) | ✓ | ✓ | ✓ | – | – | – |
| Approve work (never their own) | ✓ | ✓ | – | ✓ | – | – |
| Prepare rules / publish rules (a different person) / generate | ✓/✓/✓ | ✓/✓/✓ | – | –/✓/– | – | – |
| Upload documents / share with the portal | ✓/✓ | ✓/✓ | ✓/– | ✓/– | – | new evidence only |
| Import / review trial balances | ✓/✓ | ✓/✓ | ✓/✓ | – | – | – |
| Submissions, reminders, email dispatch | ✓ | ✓ | – | – | – | – |
| Evidence packs / briefings / CSV export | ✓/✓/✓ | ✓/✓/✓ | –/✓/✓ | ✓/✓/✓ | ✓/–/✓ | – |
| Portal staff actions (onboarding, compliance, invoices, confirm payments) | ✓ | ✓ | ✓ | – | – | – |
| Portal: own companies, messages, report payments | ✓ | ✓ | ✓ | – | – | ✓ |
| Team management | ✓ | – | – | – | – | – |
| Audit trail | ✓ | if all companies | if all companies | if all companies | if all companies | – |

**Known role conflicts (existing behaviour, kept as is; these are product decisions):**

- Auditors can add comments, so the role isn't strictly read-only.
- Directors can approve work but can't edit it, and have no portal actions.
- The same staff member can report and confirm a payment.
- "Preparer" means the last person to edit a record.
- Owner memberships can't be created through the API: each organization has one Owner.

## API reads and performance

Requests read only the records they need. The API fetches the acting profile and target record by
primary key, fetches linked records with batched or indexed queries, and applies the business rules to
that small set. Single-record actions and client views therefore cost the same whether the workspace
holds 1,000 or 100,000 records.

| Endpoint | Returns | Notes |
|---|---|---|
| `GET /api/v2` | The full workspace state the React app uses | Unchanged contract. Its size grows with the workspace by design. |
| `GET /api/v2/records` | One page of visible records: `{items, total, limit, offset}` | New. Filters: `kind`, `company`, `status` (repeat or comma-separate), `assignee`, `priority`, `due_from`, `due_to`. Sorting: `sort=updated\|due\|title\|created\|status`, `order=asc\|desc`. Paging: `limit` (1–500, default 50) and `offset`. |
| `GET /api/v2/audit` | One page of the audit trail, newest first | New. Same paging parameters. The state response only carries the newest 300 entries. |

The data model is still the original `records` table. Field filters use `json_extract` with indexes on
`(owner, company, kind)` and `(owner, kind, updated)`, which are created automatically on start.

```sh
cd backend
.venv/bin/python -m tests.perf.benchmark                     # 1k / 10k / 50k / 100k records: time, SQL count, size, memory
.venv/bin/python -m pytest tests/perf                        # guard: cost must not grow with unrelated records
```

## Sign-in and roles

Two sign-in modes (`GOVERNANCE_AUTH_MODE`), described in full in [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md):

- **`local-preview`** (development/test default): opening the app signs the browser in as the Owner of the preview organization; **Preview as** switches to another active membership of the same organization. Refused in staging/production.
- **`password`**: email + password sign-in (argon2id), invitations from **Team & access**, password reset, sign-out (and sign-out everywhere), idle and absolute session expiry, login throttling, CSRF tokens. The first owner is created with `python -m app.cli create-organization` (password typed at a hidden prompt, never invented).
- **No session:** API calls return **401**. A missing, unknown, expired or revoked cookie never falls back to the Owner.
- **Permissions:** the server checks the role and company scope on every request, whatever the UI shows; role and organization are never taken from the browser.

## Project layout

```
governance/
├── frontend/                 React source (the only place to edit the UI)
│   ├── app/                  workspace-next.tsx, client-portal.tsx, globals.css
│   ├── local/entry.tsx       React entry point
│   └── public/               static assets (see note below)
├── backend/                  FastAPI backend
│   ├── app/
│   │   ├── main.py           app factory, error handlers, reminder loop
│   │   ├── api/              routers: /api/v2, /api/v2-files, /api/v2-import, /api/portal, /api/session, static frontend
│   │   ├── services/         business workflows (records, governance, documents, intelligence, portal/*)
│   │   ├── domain/           pure rules: validation, dates, review gates, India profile, INR money, CSV parsing
│   │   ├── repositories/     SQLAlchemy data access (records, audit, sessions)
│   │   ├── models/           SQLAlchemy models
│   │   ├── permissions/      role and company-scope checks
│   │   ├── auth/             local-preview sessions
│   │   ├── storage/          local document storage
│   │   ├── database/         engine and transactions
│   │   └── core/             settings, errors, security middleware, JS-compatible value helpers
│   ├── migrations/           Alembic environment + versions/ (0001_baseline)
│   ├── tests/                pytest suite (+ e2e/ browser test, perf/ scaling guard)
│   └── alembic.ini, requirements*.txt, pytest.ini, .env.example
├── .github/workflows/ci.yml  CI: typecheck, build, pytest, compatibility checks, browser tests
├── tests/                    original API suites (run against FastAPI by pnpm test:compat) + compat/ preload
├── scripts/                  build-frontend.mjs, run-compat-tests.mjs and the original starter tooling
├── db/, drizzle/, drizzle.config.ts   original database schema + SQL migration, kept as the reference for backend/app/models
├── validation/               saved results of the original Node-era API test runs
├── legacy/                   replaced code, kept for reference only (nothing imports or runs it)
│   ├── node-backend/         the old Node.js backend, its tests, and the older v1 UI
│   ├── vinext-starter/       unused Next/vinext/Cloudflare/shadcn starter files
│   └── STARTER-README.md     the original starter README
├── .sites-runtime/           generated and runtime data — not source, gitignored
│   ├── frontend/             React BUILD served by FastAPI (written by pnpm frontend:build)
│   ├── preview/              preview.sqlite, preview.before-fastapi.sqlite, files/ (your data)
│   └── legacy-node-build/    stale build outputs of the old Node server
└── package.json, pnpm-lock.yaml, tsconfig*.json, eslint.config.mjs, postcss.config.mjs, .env*, docs
```

`frontend/` is source; `.sites-runtime/frontend/` is generated output. Edit the first, never the second.
`frontend/public/` holds the favicon and sample icons, but the API does not serve them yet, because
the original Node server did not either (the browser tab has no custom icon).

The database still uses the original generic `records` table: one JSON document per record, with `kind` naming the entity type. This keeps all existing data working. Normalising it into per-entity tables is planned for the PostgreSQL phase.

See `IMPLEMENTATION.md` for the product's features and limitations.
