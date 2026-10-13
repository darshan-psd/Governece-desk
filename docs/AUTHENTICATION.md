# Authentication and sessions

Governance Desk has two sign-in modes, chosen with `GOVERNANCE_AUTH_MODE`:

| Mode | Where | What it does |
|---|---|---|
| `local-preview` | development, test only | Opening the app signs the browser in as the preview organization's Owner; **Preview as** switches profile. Refused at start-up in staging/production. |
| `password` | any environment (required in staging/production) | Email + password sign-in, invitations, password reset, server-side sessions. |

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
