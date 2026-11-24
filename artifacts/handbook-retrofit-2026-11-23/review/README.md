# Intensive review of the handbook update

The review starts from `b46164173606eb045f11dd05ca8932f49b755ac0`. A new scratch build uses the unchanged real React source, TypeScript and Vite; historical `dashboard/dist` and browser artifacts are not overwritten. No outside customer or human acceptance reviewer participated.

## Fresh frontend checks

All **46/46** existing scenarios passed on the scratch build:

| Suite | Result | Scope |
|---|---:|---|
| [Core workbench](frontend/base/browser-results.json) | 18/18 | Precision, cursors, history IDs, retry permissions/uncertain responses, quarantine, failed publication, inventory, tenant/session races, timeout recovery, keyboard and mobile |
| [Focused races/layout](frontend/races/browser-results.json) | 5/5 | Root-URL Back navigation, null errors, late401/session ownership, 320/375/768/1440 and 200% text |
| [Neon account client](frontend/neon/browser-results.json) | 12/12 | Actual Better Auth client with fictional provider; sign-in, password clearing, JWT source, signup/enrollment, reset, logout and expiration |
| [Verification-code UX](frontend/auth-code/browser-results.json) | 3/3 | Signup, resend and existing-account guidance |
| [Contrast/keyboard follow-up](frontend/contrast/browser-results.json) | 8/8 | Exact table alternatives, clipped text and keyboard scrolling across all seven workbench views |

The core suite records 25 axe scans with zero violations. The [contrast follow-up](frontend/contrast/contrast-results.json) retains its eight axe-incomplete records and measures 134 text targets plus 18 keyboard/scroll observations. Incomplete findings are not silently erased or treated as full conformance. The 375px inventory and 1440px overview screenshots were visually inspected. TypeScript/Vite and the existing Prettier check passed. [Source-bound summary](summary.json) preserves the exact frontend hashes and scope.

Every API response in these runs is intercepted; the Neon provider is fictional. These are not fresh authenticated hosted tenant journeys. They do not contact real accounts, reset databases, send email, execute merchant effects or use paid services. One initial Windows Node process failed to initialize before executing assertions; the same source suite reran successfully and that infrastructure interruption is recorded in the summary.

## Observations and corrections

- `dashboard/README.md` incorrectly described hosted authentication as unimplemented/bearer-only and all current evidence as local. It now distinguishes the existing Neon same-origin flow, reviewed membership, separate bearer profile, local checks and recorded hosted evidence.
- A fresh advisory query found an actual selected Airflow SMTP provider advisory. The [narrow dependency correction](smtp-fix/README.md) preserves the original finding and installed snapshots, documents the one-pin fix and separates resolved metadata from the actual Linux CI gates.
- No application frontend patch was justified by the recorded UI/security review. Auth tokens remain in memory, server-maintained memberships enforce authority, React renders diagnostic text inertly, large integer values remain exact, and retry controls preserve viewer/operator and uncertain-outcome boundaries in the executed checks. These observations are scoped checks, not a zero-vulnerability guarantee.

The first advisory query contains historical optional all-extras constraints as a separate inventory. Matches in those unselected extras are not presented as packages running on Vercel. The recorded Airflow installed snapshot with SMTP provider 3.2.0 remains a failed historical graph until a matching new installed graph is collected; it is never relabeled as a 3.2.2 execution.
