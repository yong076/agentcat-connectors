# Provider policy parsing — 26.44.0 preparation

Claude Max 20x aliases now render Ultra. A plan name never establishes account
verification, and actual reported session/weekly/model windows are retained.
A weekly-only response does not synthesize a five-hour cap.

Kimi top-level usage is weekly only with an explicit seven-day window or a known
legacy plan. An unscoped/new-plan counter remains Usage with unknown duration;
explicit rate windows remain separate. Copilot explicitly reported AI-credit
counters retain their unit, without converting premium-request counters.

These parser changes complement the shared public policy catalog in DiscoverAPI;
the connector continues to own actual account data. No provider credentials,
account actions or model invocations were used for verification.

Validation: 44 focused parser/policy tests pass. The isolated full suite ran
1,079 tests and reported six existing SQLite-reader subtest failures; the same
six reproduce on unchanged HEAD. A public connector release remains a separate
owner decision; no artifact, manifest or version tag was published.
