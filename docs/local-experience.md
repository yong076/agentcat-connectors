# Optional local experience observations

The snapshot now includes `localExperience.sessions` and `localExperience.gateways`.
They are observations only: no quota claim, account action, approval reply or
additional provider token polling occurs.

- Claude hooks: SessionStart, UserPromptSubmit and PostToolUse report running;
  PermissionRequest and permission/idle notifications report attention needed;
  Stop and SessionEnd clear it. The event store uses installation-scoped opaque
  session identities. The public projection carries counts only. Missing follow-up
  observations expire after ten minutes; an idle prompt is not necessarily a
  permission request. The two-second activity response also carries these counts.
- Gateways: inspect only the selected Claude/Codex configuration. Exact HTTP
  loopback origins with explicit ports are allowed. Read the root identity first;
  only a `magpie` response unlocks its read-only today usage summary. Redirects,
  proxies, responses over 1 MiB and non-object JSON are rejected. Cache for one
  minute. Failures retain the original observation time and mark data stale.
- Forward only allowlisted vendor/model families and numeric token buckets.
  Never forward account/key/session groups, paths, origin URLs or raw model names.
  Unknown vendor/model labels become Unknown. Gateway counts are not added to CLI
  usage; connected CLI rows suppress inferred quota, burn and recommendations.
  Provider-reported subscription quota can still be displayed independently.

Run the normal sandboxed unittest suite, including `test_local_experience`.
Installing the new hooks is a separate connector update step; this change does
not modify the developer's live CLI settings during tests. The macOS client can
consume these fields additively; old clients ignore them. Fixture evidence does
not demonstrate real gateway traffic or every CLI permission mode.
