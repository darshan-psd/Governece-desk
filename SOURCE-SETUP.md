# Governance Desk — source package

This archive contains the current local application's source, including phases 2 and 3 and the India client portal with INR invoices. See IMPLEMENTATION.md for feature details, validations, architecture, and production limitations.

## Run the local application

The backend is now Python/FastAPI (see README.md for full setup). Node.js is only used to build the React frontend.

Install Python 3.11+, Node.js 24 and pnpm 11.19.0, then open a terminal in the governance directory and run:

```sh
pnpm install --frozen-lockfile
pnpm frontend:build
cd backend
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app
```

Open http://127.0.0.1:5174/#client-portal while the server is running. If that port is occupied by your existing preview, stop that preview before starting another copy.

The new installation starts with its own local database. Company records, uploaded documents, database backups, credentials, installed dependencies, and generated builds are deliberately excluded. Dependencies must be installed with internet access. Optional service variables are documented in .env.example; no external service is required for the local preview.

Local data is created under .sites-runtime/preview by default. GOVERNANCE_DATA_DIR can select a separate storage directory. Preserve and back up that directory separately if you need to retain records and uploaded files.

## Checks

```sh
pnpm typecheck                         # frontend TypeScript
cd backend && .venv/bin/python -m pytest   # FastAPI tests (isolated temporary databases)
pnpm test:compat                       # original API suites in tests/ against a throwaway FastAPI server
```

Existing validation reports from the Node backend are under validation/.

This is a local preview with simulated roles, not a production authentication system. Payments use manual reporting and secretary confirmation; no payment gateway, live statutory filing, or external notification service is configured. Compliance dates require professional review and explicit scheduling.
