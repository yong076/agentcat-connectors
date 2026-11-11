# Agent Cat local gateway lab

The owner wants to explore providing a gateway inside Agent Cat, not observing
an external magpie installation. This lab tests the smallest request boundary
without reading credentials, editing CLI settings or calling a provider.

Run `python3 scripts/run_sandboxed_tests.py` from the repository root. The
`test_gateway_lab` cases start two ephemeral loopback servers: an Agent Cat
gateway and a synthetic upstream. Both stop at test teardown. No service is
installed. `git archive` excludes the experiment and its tests.

The lab exposes `/v1/models`, `/v1/chat/completions`, `/v1/responses` and
`/v1/messages`. Each alias selects a fixture model with the same wire protocol.
Cross-protocol requests fail explicitly. Tool payloads and SSE chunks pass
through; the tests verify incremental delivery before the upstream finishes.
This is a transport feasibility test, not evidence of Claude/Codex compatibility.

Boundaries exercised: ephemeral access token, loopback bind and exact Host,
browser Origin rejection, no credential/header forwarding, request size and
concurrency limits, no redirects or retries, sanitized upstream errors,
disconnected-client recovery. The implementation has no production upstream
configuration, persistence or telemetry. Its constructor requires an in-process
fixture server. Stream errors after headers terminate the connection; production
must add protocol-specific terminal errors and a complete cancellation contract.

## Proposed product slice

Agent Cat Settings owns an optional model-routing page: client, provider,
model, protocol compatibility, and a clearly described new-session launch.
The menu bar stays focused on usage and attention; no external gateway card.
`agentcatd` owns route configuration and provider requests. A gateway worker
must have separate resource limits so long streams cannot block quota polling.

Start with an explicitly configured API-key or local-model provider and one
same-protocol CLI path. Provider credentials stay in a connector-owned private
store; clients receive only a scoped local capability. Do not silently adopt
CLI login tokens. Do not rewrite global CLI configuration. Prefer a per-session
launcher with isolated environment, plus a visible return-to-default path.

Do not promise “any model in any CLI” until protocol translations, reasoning,
tool results, multimodal input, cancellation and context limits have fixtures
and real-client evidence. An API key and a subscription are different products;
subscription bridging requires a separate provider-specific decision and must
not refresh or overwrite CLI-owned grants.

Automatic account rotation, automatic paid fallback, quota warm-up, plugins,
prompt masking and importing the upstream application are outside this slice.
A failure returns to the user instead of silently changing provider or cost.

## Reference and implementation choice

Reviewed the local magpie source snapshot: README, MIT LICENSE,
`internal/gateway/gateway.go` (routes, request translation, streaming encoders),
`internal/routes`, and provider/agent configuration structure. It is much larger
than a model selector: protocol adapters and account behavior dominate the work.
The lab is independently written; no upstream code or assets are copied.

| Approach | Benefit | Cost / decision |
| --- | --- | --- |
| Bundle the full reference | Broad existing adapters | Another runtime, updater, credential/config writer and large maintenance surface. Do not select it merely because the UI looks small. |
| Extract a bounded adapter | Reuses a tested protocol path | Requires dependency/license inventory and tests against the exact revision before adoption. Revisit when selecting the first real provider. |
| Small Agent Cat-owned worker | Fits the connector boundary and explicit opt-in | Fewer supported combinations initially. Recommended first implementation path; this lab establishes only its transport boundary. |

## Evidence still needed before a real feature

1. Pick the first supported client/provider/protocol combination; verify current
   official configuration and authentication contracts before implementation.
2. Exercise an isolated real CLI against fixtures, including tool round trips,
   cancel, upstream timeout, malformed stream, and long-running concurrency.
3. Implement opt-in credential/setup/revoke flow, safe new-session launcher,
   aggregate-only usage attribution and failure recovery. Prompts never enter
   snapshot/history/sync/telemetry. Avoid double-counting CLI and gateway usage.
4. Use an explicitly authorized real provider for compatibility and cost checks.
   Local fixtures do not establish provider terms, OAuth compatibility or cost.
5. Review direct-download packaging separately from MAS constraints. The current
   MAS embedded reader cannot acquire gateway support through this lab.

No release, provider call, account switch or live CLI configuration change is
authorized by running the lab. The gateway is not part of version 26.46.0's
advertised feature set until those acceptance steps pass.
