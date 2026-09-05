# WP30 Limits Plus report

Branch: `yong076/wp30-limits-plus`

Base: `origin/main` / connector `26.36.3`

Connector version: unchanged (`26.36.3`)

## App-facing contracts

### Codex reset credits

`GET /v1/snapshot` now preserves the opaque credit identifier needed for an explicit redemption:

```json
{
  "providers": {
    "codex": {
      "limits": {
        "resetCreditsAvailable": 1,
        "resetCredits": [
          {
            "id": "RateLimitResetCredit_…",
            "status": "available",
            "title": "One rate limit reset",
            "description": "Ready to redeem",
            "resetType": "codex_rate_limits",
            "grantedAt": "2026-08-22T00:00:00Z",
            "expiresAt": "2026-09-21T00:00:00Z",
            "redeemStartedAt": null,
            "redeemedAt": null
          }
        ]
      }
    }
  }
}
```

The optional date/status fields appear only when upstream supplies them. Account/profile ids and profile images remain excluded.

Redeem exactly one selected credit through the localhost daemon:

```http
POST /providers/codex/reset-credits/consume
Host: 127.0.0.1:8765
Content-Type: application/json

{
  "creditId": "RateLimitResetCredit_…",
  "confirmation": "one-time-app-generated-nonce"
}
```

The connector sends the nonce unchanged as upstream `redeem_request_id`, sends `creditId` as `credit_id`, and uses the same Codex bearer and `ChatGPT-Account-Id` headers as the reset-credit list request. A successful upstream POST is followed by a forced Codex limits refresh.

```json
{
  "ok": true,
  "windowsReset": 2,
  "credit": {
    "id": "RateLimitResetCredit_…",
    "status": "redeemed",
    "redeemStartedAt": "2026-09-05T06:20:00Z",
    "redeemedAt": "2026-09-05T06:20:01Z"
  },
  "limits": {
    "status": "auto",
    "quotas": [
      {
        "id": "codex:5h",
        "remainingPercent": 100.0,
        "resetAt": 1788592800
      }
    ]
  }
}
```

Local API controls:

- The existing Host-header loopback allowlist protects this route, matching `/healthz`.
- JSON content type, same-site fetch metadata, and loopback Origin checks protect the mutation from browser cross-site requests.
- Missing/invalid `creditId` or `confirmation` returns HTTP 400.
- Missing Codex OAuth credentials returns HTTP 409.
- Upstream HTTP failure returns HTTP 502 with only its status; tokens and upstream bodies are not echoed.
- The diagnostic event stores only `ok`, `windowsReset`, or a bounded reason. It stores no account id, credit id, token, or confirmation nonce.

CLI equivalent:

```sh
agentcat codex reset-credit consume 'RateLimitResetCredit_…'
```

The explicit `consume` command generates a UUID idempotency/confirmation nonce and prints the same JSON response.

### Claude extra usage and plan

`GET /v1/snapshot` adds the following fields under `providers.claude.limits`:

```json
{
  "planType": "max",
  "extraUsage": {
    "enabled": true,
    "usedUSD": 17.25,
    "monthlyLimitUSD": 100.0,
    "currency": "USD"
  }
}
```

`planType` prefers the OAuth usage payload (`plan_type`, `planType`, `subscription_type`, `subscriptionType`, or `plan`). When absent, a live refresh falls back to `~/.claude.json` → `subscriptionType`. Disabled extra usage is still emitted with `enabled: false`; it does not create a misleading monthly quota row. The existing `claude:extra_usage` quota remains for enabled accounts so older clients keep working.

### GitHub Copilot quota

When either `~/.config/github-copilot/hosts.json` or `apps.json` contains a Copilot OAuth token, the connector requests:

```http
GET https://api.github.com/copilot_internal/user
Authorization: token <local Copilot OAuth token>
Accept: application/json
Editor-Version: vscode/1.96.2
Editor-Plugin-Version: copilot-chat/0.26.7
User-Agent: GitHubCopilotChat/0.26.7
X-Github-Api-Version: 2025-04-01
```

The normalized snapshot shape is:

```json
{
  "providers": {
    "copilot": {
      "limits": {
        "status": "auto",
        "planType": "free",
        "quotas": [
          {
            "id": "copilot:premium_interactions",
            "label": "Premium interactions",
            "window": "month",
            "unit": "requests",
            "used": 50.0,
            "remaining": 450.0,
            "limit": 500.0,
            "usedPercent": 10.0,
            "remainingPercent": 90.0,
            "resetAt": 1798761600,
            "source": "https://api.github.com/copilot_internal/user"
          },
          {
            "id": "copilot:chat",
            "label": "Chat",
            "window": "month",
            "unit": "requests"
          }
        ]
      }
    }
  }
}
```

`resetAt` is connector-derived as the first day of the next month at 00:00 UTC because the API does not provide a per-quota date. Both the documented snake_case payload and camelCase equivalents are accepted. A successful quota result uses the shared 15-minute live-limit cache.

Reason behavior uses the WP25 vocabulary:

- `token_missing`: neither local file has a usable OAuth token.
- `token_expired`: GitHub returns 401 or 403.
- `not_applicable`: the authenticated account has no usable quota, including zero-entitlement token-billing placeholders. `planType` is retained when supplied.

### Adaptive provider polling

No new read endpoint is needed. Every provider live-limit reader continues through the shared per-provider cache:

- Any normalized quota with `remainingPercent < 10` changes only that provider's cache TTL to 120 seconds.
- A provider reset signal less than two hours old also uses 120 seconds. Successful Codex reset-credit consumption records such a local signal.
- Once every tracked window is at least 10%, the provider remains accelerated for 30 continuous minutes before returning to the normal TTL.
- Exactly 10% is the recovery side of the boundary.
- Retry-After and existing error-backoff behavior remain authoritative during provider failures.

The daemon's existing 60-second snapshot loop reaches the adaptive TTL boundary without adding another scheduler or a redemption path.

## Commits

- `3306fe9` — `[WP30] C1 add Codex reset-credit consume`
- `545f15c` — `[WP30] P3 emit Claude extra usage and plan`
- `fbeca86` — `[WP30] P4 add Copilot monthly quota`
- `774354f` — `[WP30] P6 add adaptive limit polling`

## Test tail

Exact command (stdin closed as required):

```sh
python3 -m unittest discover -s tests < /dev/null
```

Measured tail:

```text
Ran 447 tests in 7.998s

OK
```

Tests use redirected temporary HOME/AGENTCAT_HOME paths and stub all WP30 provider HTTP. No real provider endpoint is called.

## Exclusions

- No `CONNECTOR_VERSION` bump.
- No automatic or scheduled reset-credit consumption; only the explicit daemon POST and explicit CLI command can redeem.
- No account identity, OAuth token, credit id, or confirmation nonce in reset diagnostics.
- No Copilot browser-cookie import, optional budget scraping, OAuth login flow, or token refresh. P4 reads only tokens already written by GitHub Copilot.
- No fabricated Copilot reset date from an API field; the documented calendar-month fallback is explicit.
- No app UI, mobile upload/push, DiscoverAPI fleet detector, or remote quota-signal ingestion; those belong to the app/API slices described by the TED. P6 accepts provider-local reset timestamps, with C1 consumption wired as the current local signal source.
- No changes outside `bin/agentcat`, `tests/`, and `docs/`.
