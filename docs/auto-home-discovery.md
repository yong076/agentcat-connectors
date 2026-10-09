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
providers; CodeBuddy, Grok CLI, and Kimi Code are foreign layout owners.
Automatically detected foreign homes remain with their existing readers and
never enter Claude/Codex accounting; explicit homes follow the compatibility
rule below.

Classification evaluates all config/identity and reject fingerprints before
reading JSONL. Claude uses the home's `.claude.json`; the standard `.claude`
home falls back to HOME's `.claude.json` when its own file is absent. Codex uses
`auth.json`, OpenAI-compatible config in `config.toml`, `session_index.jsonl`, and
`state_*.sqlite` presence. Classification requires score >= 6 and a margin >= 3
over the runner-up. Reject fingerprints override positive scores; conflicting
provider fingerprints are ambiguous. A partial scan never demotes a positive
cheap fingerprint. Generic directories with no positive CLI evidence are
omitted, and confident homes appear only under their owning layout.

The configured model provider and model name do not establish foreign CLI
ownership. Codex can use Grok or Kimi models. Foreign ownership requires CLI
originators, dedicated home names, or CLI-specific config/brand files.

Default/adopted homes always retain the previous reading behavior, including
foreign/conflicting fingerprints, missing directories, and exhausted budgets.
An explicit exclusion still wins. Classification evidence remains visible.

Discovery is cached for ten minutes. At most 32 automatically discovered homes
are classified; default/adopted homes do not consume this cap. Collection,
classification, inventory and mirror comparison share one five-second deadline.
Collection additionally stops after two seconds; classification additionally
stops after 250 ms per home. An exhausted global deadline keeps the last good
scan and marks the inventory non-authoritative. Explicit adoptions/exclusions
still take effect in this fallback without candidate filesystem IO.

Automatic discovery checks cached OS mount metadata before stat/scandir and
skips network, non-local and unknown filesystems, including nested mounts,
runtime homes and launcher targets. Explicit homes retain their previous
reading behavior. All discovery filesystem IO, including path resolution and
read-only SQLite, runs in a disposable worker process. The snapshot thread
waits only until the shared deadline, terminates that worker on timeout, and
can start a fresh worker on the next scan.

Classification reads at most 64 KiB per file and the first 24 lines
of four sampled JSONL files. It visits newest Codex date directories and newest
Claude project directories first, stopping at the sample size without listing
or sorting the whole corpus. Within a leaf directory it uses scandir order.
Partial work adds `scan.partial`; no reads fit in a zero budget (`scan.budget`).
Settings mutations invalidate discovery immediately.

## States and usage

| State | Behavior |
| --- | --- |
| `tracked` | Automatically reads usage and probes; default, adopted, auto, or launcher source |
| `excluded` | User's off switch; takes precedence over automatic tracking and adoption |
| `mirror` | At least 90% of sampled/indexed session IDs belong to tracked homes; usage dedup keeps unique sessions; no separate probe |
| `foreign` | Another product owns the layout; no Claude/Codex usage or probe |
| `ambiguous` | Conflicting fingerprints; no automatic usage or probe |

Mirror discovery samples at most 256 filenames and reads only session IDs from
Codex's JSONL index (at most 8 MiB) and read-only SQLite `threads` indexes (at
most 50,000 IDs). Matching relative file paths cover copies whose IDs were not
sampled in the owner. Precedence is deterministic: default, adopted, launcher,
direct HOME, then runtime account homes and runtime aggregate homes. Larger ID
inventories precede subsets at the same priority. Explicit homes stay tracked.
Without a complete index the 90% decision is a sample estimate; tracked homes
still use the existing full usage dedup. Unsampled mirror files are checked
against their original owners during usage scanning. Unique tails are counted
under the copied home, and longer shared copies can still win.

The diagnostic `files` count is a sampled/indexed lower bound for large homes;
discovery never enumerates every rollout just to report an exact count.

Mirrors participate in the existing inode and session-ID dedup so the longer
copy still wins. Shared-session tokens belong to the original tracked home;
unique sessions belong to the copied home and its identified account. Files
without session UUIDs are counted and never falsely merged.
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
Probes deduplicate an account only after success; a failed/expired default-home
login does not suppress a working login in another home of the same account.

## Add a CLI

1. Add one registry entry with structure, brand fingerprints, identity paths,
   and usage globs. Use `foreign` for products whose existing reader owns usage.
2. Add positive and look-alike sandbox fixtures, including a conflicting/tied
   fingerprint. Ensure evidence contains no file content or credentials.
3. For a newly supported provider, update the binding provider table and add
   its usage/probe reader separately. A registry entry only classifies homes.

Run the repository's copy + fake HOME + closed stdin test command. Discovery
tests never inspect real homes, Keychain, or provider endpoints.

## Checking a real machine

`python3 scripts/discovery_dry_run.py` classifies the machine's homes read-only
(throwaway AGENTCAT_HOME, no probes, no settings writes) and prints state,
sources, launchers, evidence and timing. Run it after any discovery change and
compare against the expected outcome; fake-HOME tests alone missed real-scale
budget, candidate-order and IPC problems.
