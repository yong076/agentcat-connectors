# Automatic CLI home discovery

The connector discovers Claude and Codex homes without changing credentials or
executing launchers. The app is unchanged. Adding a product fingerprint means
adding a `HomeSignature` entry in `lib/agentcat_home_signatures.py`.

## Model and signatures

Candidates come from depth-one directories under HOME (including non-dot names),
literal `CLAUDE_CONFIG_DIR` / `CODEX_HOME` assignments in launchers in `~/.local/bin`,
`~/bin`, `/opt/homebrew/bin`, and `/usr/local/bin`, known runtime homes including
Orca account homes, and the daemon environment. HOME expansion uses the supplied
home; scripts are never executed. Symlinks and large/binary scripts are skipped.
Personal folders, caches, toolchains, and application data are denylisted for the
depth-one scan; explicit runtime and launcher sources can reach nested homes.

Each signature declares a provider, kind (`provider` or `foreign`), layout,
environment variables, default names, all/any structure markers, positive and
negative weighted fingerprints, usage globs, identity JSON paths, and runtime
globs. Supported fingerprint operations are file existence, JSON key/prefix,
JSONL field prefix, usage filename regex, and root TOML key. Claude and Codex are
providers; CodeBuddy, Grok CLI, and Kimi Code are foreign layout owners. Foreign
homes remain with their existing readers and never enter Claude/Codex accounting.

Classification requires score >= 6 and a margin >= 3 over the runner-up. Ties,
weak evidence, or an expired IO budget fail closed for automatic tracking.
Explicit default/adopted homes retain the previous reading behavior when there
is no contradictory fingerprint; a foreign match or competing strong signatures
still block them. Missing default homes stay visible for diagnosis.

Discovery is cached for ten minutes, with a five-second cycle budget and at most
32 candidates per layout. Classification reads at most 64 KiB per file, the
first 24 lines of the newest four JSONL files, and at most 50,000 filesystem
entries per traversal within a 250 ms budget. A budget-limited classification
cannot authorize tracking. Settings mutations invalidate discovery immediately.

## States and usage

| State | Behavior |
| --- | --- |
| `tracked` | Automatically reads usage and probes; default, adopted, auto, or launcher source |
| `excluded` | User's off switch; takes precedence over automatic tracking and adoption |
| `mirror` | At least 90% of session IDs already belong to earlier tracked homes; no unique usage or probe |
| `foreign` | Another product owns the layout; no Claude/Codex usage or probe |
| `ambiguous` | No confident winner; no automatic usage or probe |

Mirrors participate in the existing inode and session-ID dedup so the longer
copy still wins. Only their shared sessions survive; those tokens belong to the
original tracked home. Files without session UUIDs are never falsely merged.
Cursors rebuild when dedup survivors are removed or reassigned, so exclusions
and newly found longer copies do not leave duplicated history in totals.

`homes.<provider>.discovered[]` keeps its old fields and adds `id` (12 hex digits
of SHA-256 of realpath), `sources`, `launchers`, `state`, `evidence`, and `usage`
(`today`, `week`, `month`, `all`). HOME paths use `~`; external paths use
`~/<external-ID>` so absolute paths and usernames never appear in this block.
Evidence contains codes only. Identity extraction returns only an account hash.

Provider instances sum home usage by Claude's `oauthAccount` account ID and
Codex's probe identity (local native identity is the inventory fallback). An
observed login switch establishes a baseline: older home tokens are unknown for
the new account, and only later deltas are assigned to it. Home totals still
include that history. Unidentified and desktop-only usage stays unassigned;
external aggregate usage floors are never guessed onto an account. With fully
identified CLI-only journals, home and account sums equal the deduped total.

A launcher gives the default label `<Provider> · <launcher>`; without a launcher,
the previous label is retained. User aliases remain the app's existing override.
`agentcat homes --exclude` disables a home; `--forget` restores automatic behavior.

## Add a CLI

1. Add one registry entry with structure, brand fingerprints, identity paths,
   and usage globs. Use `foreign` for products whose existing reader owns usage.
2. Add positive and look-alike sandbox fixtures, including a conflicting/tied
   fingerprint. Ensure evidence contains no file content or credentials.
3. For a newly supported provider, update the binding provider table and add
   its usage/probe reader separately. A registry entry only classifies homes.

Run the repository's copy + fake HOME + closed stdin test command. Discovery
tests never inspect real homes, Keychain, or provider endpoints.
