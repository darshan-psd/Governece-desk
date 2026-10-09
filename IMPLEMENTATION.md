# Governance Desk — local phases 2 and 3

Updated 1 October 2026. Local application: http://127.0.0.1:5174/

This release extends the existing workspace with working governance, accounting-review and control workflows. Existing company records, work items and files are preserved. A pre-upgrade SQLite backup is retained at `.sites-runtime/preview/before-phase-2-3.sqlite`. Nothing was published or uploaded.

## What is available

| Area | Working local capability | Validation |
|---|---|---|
| Board workspace | Agenda, attendance, conflicts, minutes, follow-up tasks, review and printable evidence packs | Configured quorum excludes conflicted attendees and duplicate names; agenda, minutes and current evidence required for approval |
| Corporate events | Appointment, resignation, office change, allotment and renewal checklists | Generated tasks have sequential dependencies; every linked task must complete before event completion |
| Accounting close | Period-based CSV trial balances, immutable import versions, separate review, preparation checklists and evidence | Balanced integer-cent totals, valid period/currency, duplicate-file detection, latest reviewed import, matching close period and resolved information requests |
| Client workspace | Company-scoped requests, responses, correction cycles and shared-document access | Clients cannot edit internal work or approve responses; server checks company scope and document sharing |
| Document control | Upload/download, SHA-256 fingerprints, version chains, controlled sharing | Maximum 10 MB, allowed extensions/basic file signatures; newer versions flag dependent work and acknowledgements for review |
| Rule library | Monthly, quarterly and annual rules; day offsets; source reference; independent publication; 90-day preview and generation | Real calendar dates, clamped month ends, separate preparer/publisher, deterministic duplicate prevention and retained rule-version snapshots |
| Submissions | Manual preparation, authorisation, submitted reference, accepted/rejected acknowledgement | Approved linked task, current acknowledgement document and portal reference; filing work cannot complete without accepted evidence |
| Reports | Company completion measures, CSV register exports and immutable HTML evidence packs | Formula-safe CSV cells and HTML escaping; snapshots retain source revision and evidence hashes |
| Briefings | Local source-linked summary of overdue, upcoming and review work | Every briefing remains a draft; optional AI calls require server configuration and an explicit action |
| Reminders | In-app due/overdue inbox and acknowledgement | Checked every minute while the local server runs; one reminder per item/day; optional email requires explicit dispatch |
| Team controls | Owner, secretary, accountant, director, client and auditor profiles | Role and company checks on the server, preparer/approver separation and stale-update protection |
| Interface | Responsive navigation, list/board views, calendar, search, company filters, detail drawers, discussion/history, density and dark mode | Focus indicators, labelled controls, modal focus management, Escape, skip link and reduced-motion support |

Corporate-event templates and recurring rules are workflow aids. They contain no verified jurisdiction-specific legal deadlines. A jurisdiction was not selected, so source references and team review remain required. This is not an automatic legal-update service.

## Suggested walkthrough

1. Open **Companies** and check the company details. Sample records are labelled as illustrative.
2. Open **Team & access** to understand the local profiles. The **Preview as** selector exercises their permissions; it does not create authenticated remote users.
3. Upload company evidence in **Documents**. Open an item to download it, share it with the local client profile or create a new version.
4. Create a meeting in **Board room**, add agenda, attendance/conflicts and minutes, attach evidence, assign the preparer and save. Submit for review, switch to a different reviewer and approve. Create follow-up actions and an evidence pack as needed.
5. In **Accounting**, import a CSV with `account,debit,credit` columns. Use another profile to review the import. Create a close for the same period, link the reviewed import and evidence, complete preparation and submit for approval.
6. Create **Client requests** assigned to a client profile. Switch to that profile to respond; return as secretary/owner to accept or request correction.
7. Create a **Rule library** entry with a policy/source reference and anchor date. Submit it, publish using a different reviewer, preview and generate obligations. Generation is initiated explicitly; existing periods are skipped.
8. Use **Submissions** to record actual outcomes from an external portal. Authorisation inside this app does not perform the external filing.
9. Review **Reports**, **Briefings**, **Inbox** and **Audit trail**. Generated evidence packs download as HTML and can be printed to PDF; document bytes are downloaded separately.

## Local startup and storage

Updated 8 October 2026: the Node.js backend was replaced by a Python/FastAPI backend in `backend/` (see README.md). From this project folder:

```sh
pnpm frontend:build
cd backend && .venv/bin/python -m app
```

The frontend build uses the installed pnpm esbuild binary for the current platform. The API binds only to `127.0.0.1:5174`. `PORT` can select a separate local test port. `GOVERNANCE_DATA_DIR` can select a different SQLite/file store; the default is `.sites-runtime/preview`.

The data store comprises `preview.sqlite` and the `files/` directory. Back up both together with the server stopped, or use a consistent SQLite backup plus a matching file backup. Browser storage contains only interface preferences. Local profile session cookies are HttpOnly and SameSite Strict; sessions are stored server-side in the `sessions` table and expire after 12 hours. Requests without a valid session receive 401. They intentionally provide role simulation, not production authentication.

## Architecture

- `frontend/app/workspace-next.tsx`: React workspace and interactive modules.
- `frontend/app/globals.css`: responsive styles, dark theme, modal and focus styling.
- `backend/app/domain/`: deterministic dates, schemas, review gates, India profile formats, INR money and CSV parsing.
- `backend/app/services/`: scoped state, role checks, transitions, recurring generation, client portal, billing, reporting, audit and optional provider dispatch.
- `backend/app/api/`: FastAPI routers for `/api/v2`, `/api/v2-files`, `/api/v2-import`, `/api/portal` and `/api/session` (same paths and payloads as before).
- `backend/app/core/security.py`, `backend/app/auth/`: loopback host/origin checks, body limit and local session profiles; `backend/app/main.py` runs the reminder loop.
- `backend/app/repositories/`, `backend/app/storage/`: SQLAlchemy access to SQLite and local document storage.
- The previous Node.js implementation is kept for reference in `legacy/node-backend/`.

The existing generic `records` and `audit` tables remain compatible. Revision comparisons reject stale saves; audit entries are written with the associated record update. Documents and imports retain immutable historical versions. Evidence packs are metadata snapshots, not ZIP archives of file bytes. The append-only application history is not a legally immutable/WORM archive.

## External services and remaining production work

The **Connections** screen reports configuration, not a verified service-health check. No external messages or AI requests were sent during development or validation.

- Optional AI adapter: server variables `OPENAI_API_KEY` and `OPENAI_MODEL`; explicitly requested drafts use the Responses endpoint with `store:false`. Live provider success is untested without credentials. Source text is transmitted only if an operator explicitly requests an AI draft after configuration.
- Optional email adapter: `RESEND_API_KEY` and `MAIL_FROM`; an operator chooses a reminder recipient and dispatches it. Server-side provider errors keep the reminder unsent. Live delivery is untested without credentials.
- Regulatory filing, electronic signatures, live accounting sync, malware scanning and OCR are not connected or simulated as successful. Manual filing tracking and CSV imports are available.
- Remote team authentication/invitations, shared multi-user tenancy, production security review, verified legal rule packs, permanent scheduler infrastructure, retention enforcement, full audit export and tested disaster recovery remain production work.
- No full general ledger, tax computation, statutory-register engine or financial-statement authoring is claimed by this release. Financial close currently organises review of imported balances and supporting evidence.

Do not expose the local server to a network or treat its role selector as an access-security boundary. Production hosting was not performed or validated in this release.

## Validation

- Local frontend and API bundles built successfully.
- Targeted TypeScript validation: `node node_modules/typescript/bin/tsc -p tsconfig.local.json --noEmit`.
- Ten deterministic rule tests: `node tests/workflow-rules.test.mjs`.
- Final API validation: **204 successful request-status checks**, plus domain assertions. Machine-readable results are saved in `validation/phase-2-3.json`.
- API suite: `node tests/phases.mjs`, against a separately started server on port 5175 with `GOVERNANCE_DATA_DIR` pointing to disposable test storage. The suite creates test records and should not target the main workspace.
- Tests cover preparation, conflicting updates, approval separation, dependency cycles, quorum/conflicts, immutable packs, corporate-event gates, client/document scope, header spoofing, accounting versions, publication/generation, reminders, provider-not-configured behavior and safe exports.
- Browser checks cover responsive dashboard layouts, task creation with checklist, board-state display and client-only navigation. Existing records were inspected after upgrade. This is not a full WCAG conformance audit or a production penetration test.

UI guidance consulted: [WCAG 2.2](https://www.w3.org/TR/WCAG22/) and [Material adaptive layouts](https://m3.material.io/foundations/layout/canonical-examples/overview).

## India client portal extension — 1 October 2026

Open **Client portal** in the internal navigation, or select a Client profile in **Preview as**. Clients now land in a dedicated, company-centred workspace. Use **Onboard a client** as staff to create a company draft, its six-stage onboarding record and a company-scoped local client profile. Existing client profiles can add further proposed or existing companies themselves. These are local profiles; email invitations and production authentication are not implemented.

### Company information and onboarding

Clients can add/edit proposed or existing company names, type, CIN, incorporation date, office address, PIN, company email/phone/site, contact, business activity, PAN/TAN/GSTIN, billing address, capital, directors/DINs and shareholding percentages. Drafts can be saved before all information is available. Submission requires core contact, office and director details and an accuracy confirmation. Format checks do not constitute verification by MCA or a tax authority. The secretary verifies or returns the profile with a correction note. Editing the profile returns it to draft and flags earlier onboarding verification for review.

The onboarding journey covers engagement/company information, identity/document review, incorporation preparation, registration confirmation, post-registration setup and annual-compliance handover. Staff record progress, dates, notes and client-visible evidence. Stages must be verified in sequence; registration requires identifiers and date, and annual handover requires a scheduled compliance plan. Superseded or unshared evidence flags previously verified milestones for review.

### Document library and communication

The company library contains 14 categories spanning engagement/KYC, incorporation, MOA/AOA, registrations, officers/shareholders, registers, meeting records, financial statements, annual returns, filing acknowledgements and billing evidence. The folder list is organisational guidance; applicability varies. Official documents must be uploaded and published by the secretary. No missing certificate or filing is invented.

Clients can download each shared file at any time while the local server is running, including shared historical versions. **Download published files** creates a ZIP with actual file bytes and a SHA-256/version manifest; private files are excluded. The package limit is 200 files / 100 MB, with individual downloads available above that size. A missing file causes an explicit failure rather than a silently incomplete package. Existing upload limits and scan limitations remain.

The inbox provides company-scoped conversations, replies, read indicators, published-document attachments and staff resolution/reopening. Existing information requests are also visible and clients can respond. No email/SMS import or outbound email is implied; these are durable in-app conversations.

### Compliance calendar

The secretary can create an idempotent India annual-review checklist for a chosen financial year, schedule additional items, record applicability notes, publish selected internal tasks and attach filing evidence. Internal task notes and evidence are not automatically shared. Financial-statement filing, annual-return, meeting records, director disclosures/KYC, tax/registration and event-based review items begin **Needs scheduling**. Dates must be explicitly reviewed for the company; no blanket statutory deadlines are generated.

The calendar combines compliance dates, onboarding targets and unpaid invoices. The dashboard highlights overdue work and dates within seven days. ICS downloads include one-day display alarms, subject to the receiving calendar application's support. Alerts are in-app and calculated from stored dates; email/SMS/push delivery is not configured.

Official sources used for the checklist framing: [MCA incorporation and allied-matters FAQs](https://www.mca.gov.in/content/dam/mca/pdf/SPICEplus-and-linked-filings-FAQs-V3-20230122.pdf) and the [Companies Act on India Code](https://www.indiacode.nic.in/indiacode/handle/123456789/2114?view_type=browse). This is not an exhaustive statutory applicability engine or automated legal-update service.

### INR invoices and payments to the company secretary

Staff create service invoices with issuer name/address, client billing snapshot, whole quantities, INR unit rates, tax amount, due date and payment instructions. Drafts remain internal until issued; invoice numbers cannot be issued twice within the workspace. Issued invoices are immutable; invoices without pending/confirmed payments can be voided with a reason. Amounts use integer paise. Tax is explicitly entered and reviewed by the issuer, not calculated as an assumed GST rate. The HTML invoice/receipt downloads are printable and are not a claimed GST e-invoicing/IRN integration.

Clients pay outside the app using the secretary's instructions, then report an amount, date, transaction reference and proof. Reporting never transfers funds and does not mark an invoice paid. Staff confirm or reject the report. Partial payments, pending amounts, remaining balances and receipts are supported. Overpayments, future dates, duplicate transaction references and client-side confirmation are rejected. Pending amounts reserve the remaining invoice balance. Only confirmed reports produce downloadable receipts.

### Reference and validation

Design and navigation were informed by the accessible login page and the provider's public [dashboard guide](https://help.corporateservices.com/docs/using-the-csp-dashboard), [navigation guide](https://help.corporateservices.com/docs/navigating-in-csp), and [billing guidance](https://help.corporateservices.com/docs/payments). The authenticated reference application was not accessed. No branding, images or proprietary implementation was copied.

Final checks: 121 portal API request checks and 204 existing-workflow regression checks passed, alongside TypeScript and ZIP-content verification.

New source (at that time): `frontend/app/client-portal.tsx` and, in the since-replaced Node backend (now under `legacy/node-backend/`), `lib/portal-domain.ts`, `lib/portal-service.ts`, `lib/zip-store.ts` and `app/api/portal/route.ts`. Existing local data was backed up to `.sites-runtime/preview/before-client-portal.sqlite` before changes.

Validation scripts: `tests/client-portal.mjs` and the existing `tests/phases.mjs`, run only against a separate test database. Reports are saved in `validation/client-portal.json` and `validation/phase-2-3-regression.json`. ZIP integrity and exact public-file bytes were separately verified. Browser checks covered desktop/mobile dashboard presentation, full client company details, client message submission and staff onboarding creation. TypeScript validation passed for the new modules. Live provider, statutory filing, gateway payment, remote login and round-the-clock hosting are outside this local release.
