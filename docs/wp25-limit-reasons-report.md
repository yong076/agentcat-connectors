# WP25 live-limit reason report

Two live-limit bugs found on a real Mac (2026-09-05) on connector 26.36.2.

## Changed functions

### 1. Kimi empty stub credentials (`dafd8c7`)

Kimi CLI 0.39.x writes `credentials/kimi-code.json` as a stub (`access_token: ""`, `expires_at: 0`) and stores the live token in sibling `credentials/kimi-code-env-<hash>.json` files (same shape: `access_token`, `refresh_token`, `expires_at` in seconds, `expires_in`, `scope`, `token_type`).

| Function | Change |
| --- | --- |
| `kimi_credential_files` | New. Collect every `credentials/kimi-code*.json` across `kimi_roots()`. |
| `kimi_expires_at_epoch_seconds` | New. `expires_at` &lt; `10**11` is seconds; otherwise milliseconds. Zero is preserved as an expired/stub sentinel. |
| `_kimi_token_from_payload` | New. Read `access_token` / `accessToken` and `refresh_token` / `refreshToken`. |
| `_kimi_credentials_from_file` | New. Skip files with neither access nor refresh token. |
| `kimi_credentials_path` | Return the file with the latest `expires_at` among files that have a token; fall back to the stub path. |
| `read_kimi_oauth_credentials` | Scan all candidate files, skip empties, pick the newest `expires_at`. `reason: token_missing` only when no file has any token. |
| `kimi_live_limits` | Compare expiry in seconds. `reason: token_expired` when refresh fails. |

### 2. Gemini / Antigravity 403 without a Code Assist project (`98372c1`)

A valid `~/.gemini/oauth_creds.json` with no `GOOGLE_CLOUD_PROJECT` / local project id called `retrieveUserQuota` and got HTTP 403. `live_limit_error` mapped that to `token_expired`. Antigravity previously aborted with `error: "Antigravity Code Assist project ID unavailable"`.

| Function | Change |
| --- | --- |
| `live_limit_error` | Optional `project_id`. HTTP 403 with an explicitly empty project id → `reason: "project_required"`. HTTP 401 stays `token_expired`. Callers that omit `project_id` (Claude, Codex, …) still map 403 → `token_expired`. |
| `serve_stale_live_limits` | Forwards `project_id` so `liveErrorReason` is `project_required` rather than `token_expired`. |
| `write_live_limits_cache` | Never replace last-good `status: auto` quotas with an error-only payload; attach `liveError` + `liveErrorReason` instead. |
| `fetch_code_assist_quota` | When project id is empty (or the project-scoped call has no buckets), still call `retrieveUserQuota({})` before failing. A 403 from that call bubbles up for classification. |
| `gemini_live_limits` | Resolve `project_id` before the probe and pass it into the error / stale paths. |
| `antigravity_live_limits` | Same, and on the empty-limits path set `error: "Antigravity Code Assist project ID unavailable"` when `reason` is `project_required`. |

Connector version was not bumped.

## Test output tail

```text
$ python3 -m unittest tests.test_wp25_limit_reasons < /dev/null
...........
----------------------------------------------------------------------
Ran 11 tests in 0.016s

OK

$ python3 -m unittest discover -s tests < /dev/null
...
----------------------------------------------------------------------
Ran 431 tests in 7.645s

OK
```

WP25 coverage: stub + env file finds credentials; two env files pick the newest `expires_at`; seconds vs milliseconds parsing; stub-only → `token_missing`; refresh failure → `token_expired`; 403 + no project → `project_required`; 401 → `token_expired`; `retrieveUserQuota({})` success populates quotas; 403 with a cached quota keeps `status: auto` + `liveErrorReason: project_required`; Antigravity empty-limits path sets the project-unavailable `error` string.

## JSON reason fields the app should map

Live-limit payloads expose two machine-readable fields. They are not interchangeable.

| Field | When it is set | Where to read it |
| --- | --- | --- |
| `reason` | Empty / failed probe with no last-good quotas. `status` is `error` or `not_configured`. | Top-level `providers.<id>.limits.reason` (and the live-limits cache `limits.reason`). |
| `liveErrorReason` | Probe failed but last-good quotas are still shown. `status` stays `auto`. | Top-level `providers.<id>.limits.liveErrorReason`. `error` / `liveError` is the raw exception string and must not drive UI copy. |

Suggested app-side `reason` / `liveErrorReason` → action table:

| Code | Typical `status` | Meaning | App action |
| --- | --- | --- | --- |
| `token_missing` | `not_configured` | No credential file has an access or refresh token. | Ask the user to log in to that CLI (`kimi`, `gemini`, …). |
| `token_expired` | `error` (or `auto` + stale quotas) | Token present but refresh failed, or HTTP 401 (and 403 that is not a missing-project / missing-scope case). | Ask the user to re-auth. Do **not** send them to create a GCP project. |
| `project_required` | `error` (empty path) or `auto` + `liveErrorReason` (stale path) | OAuth is valid; Code Assist has no project. Gemini/Antigravity HTTP 403 with an empty project id. | Prompt to set `GOOGLE_CLOUD_PROJECT` or create/select a Code Assist project. Keep showing cached quotas when `status` is `auto`. |
| `missing_scope` | `error` | HTTP 403 whose body mentions `user:profile`. | Re-auth with the missing OAuth scope. |
| `rate_limited` | `error` or `auto` + stale | HTTP 429. `retryAt` is epoch seconds. | Back off until `retryAt`; keep cached quotas if present. |
| `server_error` | `error` | HTTP ≥ 500. | Retry later. |
| `network_error` | `error` | `URLError` / `OSError` / `TimeoutError`. | Retry when the network is back. |
| `parse_error` | `error` | Quota JSON could not be parsed. | Treat as a connector bug, not a user-auth problem. |
| `not_applicable` | `not_configured` | Account answered the quota API with no buckets. | Hide the quota tile; do not nag for login or a GCP project. |
| `http_error` | `error` | Other HTTP status. | Generic retry. |
| `unknown_error` | `error` | Unclassified exception. | Generic retry. |

Concrete Gemini/Antigravity shapes after this fix:

Empty probe, no cached quotas:

```json
{
  "status": "error",
  "reason": "project_required",
  "error": "HTTP Error 403: failed"
}
```

Antigravity empty probe additionally sets:

```json
{
  "status": "error",
  "reason": "project_required",
  "error": "Antigravity Code Assist project ID unavailable"
}
```

Stale quotas kept after a 403:

```json
{
  "status": "auto",
  "quotas": [{"id": "gemini:pro", "usedPercent": 10.0, "remainingPercent": 90.0}],
  "liveError": "HTTP Error 403: failed",
  "liveErrorReason": "project_required"
}
```

HTTP 401 (token actually dead), empty path:

```json
{
  "status": "error",
  "reason": "token_expired"
}
```

Kimi shapes:

```json
{"status": "not_configured", "reason": "token_missing"}
{"status": "error", "reason": "token_expired"}
```

Map `reason` when present on an empty/error payload; map `liveErrorReason` when `status` is `auto` and quotas are still populated. Never treat `liveError` / `error` text as the reason code.
