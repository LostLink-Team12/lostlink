# LostLink frontends (D2 / D3)

One React + Vite + TypeScript project, two HTML entries sharing transport,
authentication and UI code. No backend files or contracts are changed.

```powershell
cd frontend
npm ci
Copy-Item .env.example .env.local
# Fill in public dev configuration in .env.local.
# Run dev, tests and build in separate terminals or stop dev before continuing:
npm run dev
npm test
npm run build
```

Requires Node.js 22.12+. Open http://127.0.0.1:5173/venue-portal/ or
http://127.0.0.1:5173/owner-app/. Build output is `dist/` with both entry directories;
serve it from S3 + CloudFront or another static host. The root `/` is not an entry.

## Backend and Cognito configuration

API calls use the generated backend contracts merged into `main` in PR #1.
The backend processes must still be deployed and configured separately.
`.env.example` ports are examples, not service
discovery. For local development, the example uses `/api/venue`, `/api/item`,
`/api/matching` and `/api/claim`; Vite forwards these prefixes to the corresponding
`DEV_*_API_TARGET` and preserves the backend `/v1` path. Set targets to the ports
where your backend processes actually run. The Vite proxy applies only during dev.
Both apps can use the same public Cognito app client or separate public clients.

For deployment, fill in `public/config.json` before building, or edit `dist/config.json`
after building. Nonempty runtime values override the corresponding `VITE_*` values;
empty values fall back to the build-time settings. Runtime settings are fetched
without browser caching on page load, so changing them does not require rebuilding.
Configure the hosting/CDN to revalidate `/config.json` as well. API values are service
base URLs without `/v1`, or same-origin paths routed by your production gateway.
Only public URLs and client IDs belong in this file, never client secrets or tokens.

Enable authorization-code flow with S256 PKCE, `openid email profile` scopes, no client
secret, and these exact callback/sign-out URLs for local development:

- `http://127.0.0.1:5173/owner-app/`
- `http://127.0.0.1:5173/venue-portal/`

Register corresponding HTTPS URLs for deployed hosts. Backend Cognito client allowlists
must include these public client IDs. Venue staff need synced tenant membership;
owners need a verified email to claim. New venue onboarding requires a platform admin
and an existing Cognito subject for the initial administrator.

Configure API/gateway CORS for the frontend origin, `Authorization`, `Content-Type`,
`Idempotency-Key`, `If-Match` and GET/POST/OPTIONS. S3 CORS must allow POST from this
origin; the browser posts the exact presigned fields with the file last and sends no
Cognito token to S3. Workers and embedding consumers must be running.

The apps exchange the code directly at Cognito. Only PKCE state/verifier is held in
sessionStorage during login; access tokens stay in memory and expire automatically.
Reloading requires sign-in again (Cognito can reuse its login session). Sign-out
clears local state and redirects through Cognito logout. No password/token entry box,
ID token authentication, refresh token storage or caller-selected tenant is used.

## Implemented flows

- Venue: Cognito login; platform-admin venue onboarding; register a found item and
  private verification question; upload/confirm a photo; poll the operation; paginated
  item list; review claim answers and approve with version checks and completion polling.
- Owner: text and/or photo search; category, venue and date filters; similarity shown
  as a cosine score; save lost reports; paginated reports and report matches; answer
  public verification questions; submit a claim with optional contact details; view
  claim status and obtain the handover payload after approval.
- Errors remain visible, buttons disable while work is pending, and uncertain failed
  writes reuse their idempotency key. Registration retains its item/upload progress
  in memory for retry; keep the tab open with the same draft until completion.

This implementation does not provide a staff management console, rejection/cancellation
UI, QR scanner or full handover UI. Handover payload is currently displayed as text.
Email `/matches/{id}` deep links are not implemented; the current backend contract
has no public single-match endpoint. Owners can view matches from their report list.
These are remaining UI/integration limitations, not proof of cloud end-to-end
acceptance. No demo-only authentication bypass is shipped.

## Verification

`npm test` covers Cognito PKCE/state checks, transport headers/errors, photo validation,
raw photo base64, S3 credential separation, retry behavior, polling, owner claim flow
and staff approval, plus runtime settings and local proxy configuration.
`npm run build` type-checks and builds both entries. Tests mock
network responses; real Cognito, CORS, S3, CLIP and AWS dev require integration testing.
