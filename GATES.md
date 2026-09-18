# Gates: TRA-1570

Scope: Connector native account sync identities and snapshot privacy.

- [x] G1: Codex, Claude, Gemini, and supported Antigravity ID tokens use native IDs; matching accounts share identities across homes/devices, different accounts differ, and missing IDs remain profile_only.
  CHECK: python3 -m unittest discover -s tests -p test_home_discovery.py
  EXPECT: OK
  EVIDENCE: 67 tests passed. NativeSyncIdentityTests covers distinct fixture homes, hostnames, device keys, missing/invalid IDs, issuer checks, and persisted/HTTP snapshot privacy. Claude accountUuid and Gemini Google sub field presence verified locally without printing values.

- [x] G2: Connector regression suite passes, including full snapshot privacy.
  CHECK: env CODEX_HOME= CLAUDE_CONFIG_DIR= python3 -m unittest discover -s tests -p '*test*.py'
  EXPECT: OK
  EVIDENCE: 601 tests passed. Python compilation of bin/agentcat, scripts/install.py, and scripts/public_channel_install.py passed. git diff --check passed. InsightsIntegrationTests now redirects all paths with the existing sandbox helper to exclude live Antigravity state.

- [x] G3: Only connector changes staged; ticket completed with verification evidence.
  EVIDENCE: Staged README.md, bin/agentcat, tests/test_agentcat.py, tests/test_home_discovery.py, and this ledger in the sync-identity connector worktree. control-ticket.mjs done TRA-1570 returned [done] with ID 61a8de54-8884-41ef-aab3-200736e916d1. No deployment or changes to agent-cat/agentcat-api.
