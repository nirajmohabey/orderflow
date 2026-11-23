# OrderFlow dashboard

A React operations workspace for the tenant-scoped OrderFlow API. The built client is served by FastAPI at `/`; development uses a Vite proxy to the owned local API on port 8010.

## Run

Use a current Node.js version supported by Vite 8 (verification used Node 26.13.1). All direct dependencies are exact versions and the npm lockfile is committed with the project.

```powershell
cd outputs/orderflow/dashboard
npm ci
npm run build
```

Start the API through the root project's reviewed local launcher. It serves `dashboard/dist` and its local assets. Rebuild before starting an API process for the first time so the static mount exists.

For development, start the API on 127.0.0.1:8010, then run `npm run dev` here. Vite listens on 127.0.0.1:5174 and proxies `/v1` to that API. No cloud service or external font is required.

For this local Vite workflow, set `ODF_ALLOWED_ORIGINS=http://127.0.0.1:8010,http://localhost:8010,http://127.0.0.1:5174,http://localhost:5174` in the ignored local `.env` and restart the API before signing in. The built dashboard at port8010 uses the default allowlist. Production uses its own exact HTTPS origins.

## Workspace behavior

- Overview shows the latest entity snapshots, source counters, committed freshness, and pipeline attempts. Current order amounts count each latest order once; they are not cumulative sales. Pending work sums pending and retry-wait states. Inbox filters preserve the exact state distinction.
- Current orders and products use authenticated keyset pagination. Product history retains raw revisions alongside curated half-open effective intervals. The SVG price chart uses those intervals; exact values stay in the adjacent table. Opaque product IDs, including dot segments, slashes, and Unicode, are sent using the query-form history endpoint.
- Event detail exposes bounded processing metadata and integrity hashes. Only a server-verified operator sees the retry form, and only on a retry-wait event. A lost acknowledgement triggers a fresh event read before another action can be offered. Terminal quarantines explain the new-identity/new-version correction process and have no retry control.
- Runs and reports distinguish no successful publication, a last-good result, a failed latest attempt, and age-based staleness. Bounded previews are labeled, with local artifact provenance and checks available for inspection. Staleness does not automatically imply that the latest attempt failed.
- Inventory comparison preserves failed results, missing-row presence, individual SKU differences, and the independent snapshot/reference identities. A zero net difference is not presented as a per-SKU match. Inventory comparison does not mutate entity records.
- Loading, empty, unavailable, retained-prior-view, expired-session, and request-ID states are explicit. Requests have a 20-second deadline. An unavailable read does not replace a previous successful view with manufactured zeros.

## Session and precision boundaries

The server must enable both local environment and demo configuration before the fixture identity picker appears. Other environments offer a manually supplied bearer token; this is not an implemented external identity-provider flow. The UI verifies `/v1/session` and uses its principal and tenant rather than trusting decoded token claims.

Tokens stay in React memory. Tenant switches immediately remove the previous workspace, abort reads, and reject delayed responses. Disconnect and reload clear the session. Source credentials are never requested or displayed by the client. API authorization remains the enforcement boundary for every operation.

JSON integers outside JavaScript's safe range are parsed into exact decimal strings with `lossless-json`. Entity versions and aggregate amounts are not rounded. Currency formatting uses BigInt minor-unit arithmetic, including fractional currency digits. Chart conversion to Number is limited to individual product prices, which the typed API bounds below JavaScript's safe integer limit.

## Verification

```powershell
npm run build
npm run check:format
npm audit
npm run test:browser
npm run test:live
```

`test:browser` launches installed Google Chrome against the **built** client with isolated intercepted API responses. It verifies precision, keyset/filter resets, history identifiers, operator/viewer controls, terminal quarantine, failed/absent publications, inventory differences, delayed tenant/detail responses, 401 cleanup, retained prior data, lost retry acknowledgements, RAM-only tokens, a real timeout/recovery path, and 375/768/1440 layouts. It writes `../artifacts/dashboard/browser-results.json`, screenshots, source hashes, and axe results. Its retry proof is explicitly a browser/API-interception test.

`test:live` uses actual HTTP and PostgreSQL at 127.0.0.1:8010. It is restricted to loopback and performs read-only domain operations plus gated fixture sign-ins. It checks serving fixture counts/totals before and after, records video/screenshots, and scans the real UI at 375/768/1440. It writes `../artifacts/dashboard/live/live-results.json`. It never seeds, resets, or ingests serving records, and does not manufacture a retry-wait fixture. Backend acceptance tests separately verify real retry authorization and transactional semantics.

Both scripts require the system Chrome installation. For video capture only, `npx playwright install ffmpeg` installs Playwright's private helper. No browser or operating-system installation is necessary when Chrome is already available.

Reports record source hashes and source stability. Treat intercepted-browser, actual-HTTP, accessibility-state, and backend acceptance counts as different evidence scopes. Current evidence is local synthetic execution, not a hosted production/customer claim.
