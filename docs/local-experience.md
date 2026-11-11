# Optional local session observations

The snapshot includes `localExperience.sessions` and the two-second activity
response includes `attentionSessions`. These are observations only: no quota
claim, account action, approval reply or provider request occurs.

Claude hooks: SessionStart, UserPromptSubmit and PostToolUse report running;
PermissionRequest and permission/idle notifications report attention needed;
Stop and SessionEnd clear it. The event store uses installation-scoped opaque
session identities. The public projection carries counts only. Missing follow-up
observations expire after ten minutes. An idle prompt is not necessarily a
permission request.

Run the sandboxed unittest suite, including `test_local_experience`. Installing
new hooks is a separate connector update step; tests do not modify live CLI
settings. Older clients ignore the additive fields. Non-interactive CLI modes
still need separate real-service evidence.

External gateway discovery and display were removed after the owner clarified
that the intended exploration is an Agent Cat-owned gateway. See the separate
fixture-only gateway lab; it is not installed, started or imported by agentcatd.
