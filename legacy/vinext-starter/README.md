# Deprecated Next / vinext / Cloudflare starter files

The project began from a "vinext" starter (Next.js-style app on Cloudflare Workers with D1/R2 and shadcn/ui).
The delivered application never used most of it: the real React app is built by `scripts/build-frontend.mjs`
from `frontend/`, and the API is `backend/`. These files were moved here after confirming that nothing
in the active build, the typecheck, the FastAPI backend or any test imports them.

| File / folder | Why it is unused |
|---|---|
| `vite.config.ts` | Needs `.openai/hosting.json` and four files under `build/` that are not in the project. `pnpm dev`/`build` failed with "Module not found" even before the move, so those scripts were removed from `package.json`. |
| `next.config.ts`, `app/layout.tsx`, `app/page.tsx` | Next/vinext entry points. The real entry is `frontend/local/entry.tsx`. |
| `app/chatgpt-auth.ts` | Sign-in-with-ChatGPT helper for the hosting platform; imported by nothing. |
| `components/`, `components.json`, `hooks/`, `lib/utils.ts`, `vendor/` | shadcn/ui library (61 components). Only `components/*` itself imports it. The active UI uses none of it. `vendor/` is shadcn's stylesheet; no CSS imports it. |
| `lib/connector*.ts(x)`, `lib/connector-*.mts`, `lib/connector-preview.d.ts`, `components/connector-error.tsx` | Sites "connector" plumbing for the hosting platform. Only `vite.config.ts` (via files that are missing) and each other reference them. |
| `cloudflare-env.d.ts` | Types for Cloudflare D1/R2 bindings; no longer used by the typecheck. |

To revive any of it, move it back to the project root; every relative import inside is unchanged.
