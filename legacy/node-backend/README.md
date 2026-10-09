# Deprecated Node.js backend

These files are the original Node.js backend of Governance Desk. They were replaced by the
FastAPI backend in `backend/` and are kept only for reference. Nothing builds, imports or runs them.

| Former path | Replaced by |
|---|---|
| `local/server.ts` (HTTP server, sessions, host/origin checks, reminder timer) | `backend/app/main.py`, `app/core/security.py`, `app/auth/` |
| `local/adapter.ts` (SQLite + local files behind D1/R2-shaped interfaces) | `backend/app/database/`, `app/repositories/`, `app/storage/` |
| `app/api/v2`, `v2-files`, `v2-import`, `portal` route handlers | `backend/app/api/` |
| `lib/phase-service.ts`, `lib/portal-service.ts` | `backend/app/services/` |
| `lib/workflow-rules.ts`, `lib/portal-domain.ts`, `lib/accounting.ts` | `backend/app/domain/` |
| `lib/server.ts`, `lib/zip-store.ts` | `backend/app/repositories/`, Python `zipfile` |
| `app/api/workspace`, `files`, `import` (older routes, already not served) | — |
| `lib/domain.ts` (sample data + simple transition rules of the older v1 route) | — (superseded by `backend/app/domain/`) |
| `app/workspace.tsx` (older v1 UI for `/api/workspace`) | `frontend/app/workspace-next.tsx` |
| `scripts/local-preview-build.mjs` | `scripts/build-frontend.mjs` (frontend only) |
| `tests/workflow-rules.test.mjs`, `tests/domain.test.mjs` | `backend/tests/test_rules.py` |
| `tests/integration.mjs` (targeted the old `/api/workspace` route) | `backend/tests/` |

`tests/phases.mjs` and `tests/client-portal.mjs` were not moved: they still run, unchanged, against
FastAPI with `pnpm test:compat`.

`lib/domain.ts` and `app/workspace.tsx` (the v1 UI) used to sit in the project root only because they
import each other. They now live here, next to the v1 route and test that use them, so
`tests/domain.test.mjs` resolves `../lib/domain.ts` and `../lib/accounting.ts` again.
