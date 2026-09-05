# Reflect human-session provenance (26.36.9)

A human Claude Code conversation can contain background worker completion messages.
Previously, any system, hook, or task-notification entry marked the entire journal
as automated, excluding the owner's prompts from prompt-quality averages.

The first substantive prompt now supplies the session's SDK/agent provenance.
System, hook, and task-notification messages are excluded from human prompt counts
and analyzer input. Assistant replies, tool counts, and token accounting are retained.
SDK/agent-initiated sessions and the existing Orca worker-directory rule remain
automated; Codex classification is unchanged.

The first database open after upgrading invalidates Claude index cursors once.
The next normal sync reclassifies unchanged journals. Existing analyses are preserved
and their cache coverage follows a corrected smaller prompt bucket, so nightly
analysis does not rerun solely because of the migration. Notification-only journals
remain as counts-only records and do not contribute cached coaching to weekly
rollups. No analyzer calls are started by the migration. Previous coaching may still
reflect the old sampled input until an analysis is explicitly regenerated.
This one-time rebucketing keeps existing coaching even if some new human turns
arrived before the first sync, provided the corrected prompt count is smaller.

Regression coverage uses only synthetic transcripts: human sessions with worker
notifications, SDK/agent sessions, notification-only journals, unchanged-file
reindexing, analysis preservation, and the normal skip-on-unchanged fast path.

Validation: the full sandboxed suite passes (577 tests), as does Python compilation.
A read-only comparison on a local interactive journal changed automation to false
and removed injected notification prompts while preserving tool and token totals.
No transcript contents or account identifiers are included in these fixtures.
