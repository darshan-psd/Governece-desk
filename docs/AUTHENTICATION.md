# Authentication and sessions

Governance Desk has two sign-in modes, chosen with `GOVERNANCE_AUTH_MODE`:

| Mode | Where | What it does |
|---|---|---|
| `local-preview` | development, test only | Opening the app signs the browser in as the preview organization's Owner; **Preview as** switches profile. Refused at start-up in staging/production. |
| `password` | any environment (required in staging/production) | Email or username + password sign-in, invitations, password reset, server-side sessions. |

## Why built-in authentication

Two options were evaluated:

* **Managed identity provider** (Auth0, Clerk, AWS Cognito, Microsoft Entra ID, or self-hosted
  Keycloak). These add MFA, SSO and breached-password detection with little code. They are paid per
  active user (except self-hosted Keycloak, which is another service to run), the identity data
  sits with a third party (a data-residency question for Indian client data), and the app would
  need an OIDC callback flow.
* **Built-in** (chosen). No paid provider, no change to the architecture, and identities stay in
  the application database next to the Phase 2 memberships. The cost is that the application
  must implement the security controls itself; they are listed below and covered by tests.

Moving to OIDC later is a contained change: a provider only has to call
`app.auth.issue_session(services, membership_id, method="oidc")` after verifying the user, and
map the provider's subject to `users.id`. MFA (TOTP) is **not implemented** yet (see "Known limitations" below).

## Controls

| Control | Implementation |
|---|---|
| Password storage | argon2id (`argon2-cffi` defaults, RFC 9106 profile 2). Plain passwords are never stored, logged or emailed. |
| Password policy | 12–256 characters, at least 5 distinct characters, not a common password, must not contain the name or email local part. |
| Sign-in failures | One generic message for unknown email, wrong password, inactive user. A full hash runs even for unknown accounts (timing). |
| Throttling | Database-backed (works across processes): 5 failures per account and 30 per client address within 15 minutes → HTTP 429. Password-reset requests: 5 per address per hour. |
| Sessions | Opaque 256-bit token in an HttpOnly, SameSite=Strict cookie; only its SHA-256 is stored. Absolute lifetime (`GOVERNANCE_SESSION_HOURS`), idle timeout (`GOVERNANCE_SESSION_IDLE_MINUTES`), server-side revocation. |
| Cookie names | `__Host-gov_session` / `__Host-gov_csrf` in staging/production (HTTPS only, exact host, `Secure`), `gov_session` / `gov_csrf` locally. |
| CSRF | Every POST/PUT/PATCH/DELETE to `/api/` needs `X-CSRF-Token` equal to the `gov_csrf` cookie (double submit). For signed-in requests the token must also be the session's own token, `sha256("governance-csrf:" + session token)`, so a token planted by another site cannot be used. The React app adds the header automatically (`frontend/app/sign-in.tsx`). |
| Revocation | Sign-out, sign-out everywhere, per-session revocation, password change (other sessions), password reset (all sessions), member deactivation (all sessions of that membership). |
| Membership validation | Every request re-reads the membership: inactive organization, user or membership → 403; sessions of other sign-in methods (preview sessions) are worthless in password mode. |
| Browser input | Role, organization, actor ids in bodies or headers are ignored; the server derives them from the session. **Preview as** is disabled (404) in password mode. |
| Audit | `security_events` table (sign-in, failures, throttling, resets, invitations, activation, deactivation, revocation) plus organization audit entries for organization-scoped events. Owners can read their organization's events at `GET /api/auth/security-events`. |

## API

| Endpoint | Who | Purpose |
|---|---|---|
| `GET /api/auth/config` | anyone | `{"mode": ...}`; issues the anonymous CSRF cookie |
| `POST /api/auth/login` | anyone | `{email, password, organization?}` → session cookie, or `{"ok": false, "chooseOrganization": [...]}` when the user belongs to several organizations |
| `GET /api/auth/me` | signed in | user, organization, membership, session |
| `POST /api/auth/logout` | signed in | `{everywhere?: true}` |
| `GET /api/auth/sessions`, `POST /api/auth/sessions/revoke` | signed in | own sessions only |
| `POST /api/auth/password/change` | signed in | `{current, password}`; ends the user's other sessions |
| `POST /api/auth/password/forgot` | anyone | `{email}`; always the same answer |
| `POST /api/auth/password/reset` | link holder | `{token, password}` |
| `POST /api/auth/activate` | link holder | `{token, password}` |
| `GET /api/auth/team`, `POST /api/auth/team/invite`, `POST /api/auth/team/status` | Owner | sign-in status, invitation, deactivation/reactivation |
| `GET /api/auth/security-events` | Owner | paginated, organization-scoped |

## Client team members

A Company Admin creates accounts for their own company from **Team Members** in the client portal.
There is no email, invitation or activation step: they choose a username and an initial password and
give them to the person, who signs in straight away. The sign-in form takes an email or a username
(`POST /api/auth/login` with `identifier`; `email` still works). A username never contains `@`, is
unique across the whole deployment regardless of case, and is throttled and hashed exactly like
email sign-in. Someone who signs in with a username has no reset email: their Company Admin, or the
firm's Owner or Secretary, sets a new password, which ends all their sessions.

Everyone on a client's team is a `Client` membership, which keeps them out of all internal work. Their
role inside a company is on `company_access` (`client_role`, `client_modules`), one row per company.
These roles are separate from the firm's own Director and Accountant. `client_role` NULL is a company's
primary client contact, who acts as its Company Admin, so accounts from before this feature are
unchanged. The rules live in `app/domain/client_access.py` and are enforced in `app/permissions`,
so they hold on every route to the data (`/api/portal`, `/api/v2`, file downloads and exports):

| Role | Sections |
|---|---|
| Company Admin | Everything, including Team Members, editing the company profile and uploading |
| Director | Work Tracker, company profile (view), onboarding, documents, compliance calendar |
| Accountant | Financial documents, invoices and payments, uploading (payment proof) |
| Employee | Only the sections assigned to them; `invoices` also allows uploading payment proof |

Everyone also has Overview, the requests addressed to them, the documents sent for those requests
and whatever they uploaded themselves.

Two rules in `app/services/team_service.py` protect other people: account-level changes (name,
username, password, deactivation) need authority over every company the person belongs to, while
their role in this company and removing them from it need only this company; and a company always
keeps an active Company Admin, checked under a row lock.

| Endpoint | Purpose |
|---|---|
| `GET /api/portal/team?company=` | The company's team and counts |
| `POST /api/portal/team/create` | Create an account |
| `POST /api/portal/team/update` | Edit details and role |
| `POST /api/portal/team/password` | Set a new password |
| `POST /api/portal/team/status` | Deactivate or reactivate |
| `POST /api/portal/team/remove` | Remove from this company (closes the account if it was their only one) |

## Bootstrapping the first owner

From `backend/` with the target environment's variables set (`GOVERNANCE_AUTH_MODE=password`):

```bash
# New firm; the owner's password is typed at a hidden prompt (never on the command line):
.venv/bin/python -m app.cli create-organization --id firm-a --name "Firm A" \
    --owner-name "Asha Rao" --owner-email asha@firm-a.example

# ...or email the owner an activation link instead of typing a password:
.venv/bin/python -m app.cli create-organization --id firm-a --name "Firm A" \
    --owner-name "Asha Rao" --owner-email asha@firm-a.example --invite

# Give an existing profile (for example the preview workspace's owner) an email and password:
.venv/bin/python -m app.cli set-credentials --organization local-preview --member owner --email you@example.com
```

The owner then invites the rest of the team from **Team & access → Invite**. Each person sets their
own password from the single-use link (valid `GOVERNANCE_ACTIVATION_HOURS`, default 72 h).

In development the default mail provider is `outbox`: emails are written to
`<data dir>/mail-outbox/*.json`, so links can be followed without a mail server.

## Known limitations

* No MFA yet (recommended before production use with client data).
* A person who belongs to two organizations needs one user record; invitations refuse an email
  that already belongs to another account. Linking an existing user to a second organization is
  done by an operator today.
* Email delivery for invitations and resets is synchronous within the request; it moves to the
  background job queue in Phase 7.
* A Company Admin knows the initial password they set. `POST /api/auth/password/change` lets a member replace it, but the portal has no screen for that yet.
