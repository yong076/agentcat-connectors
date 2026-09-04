from ._base import HomeSpec, ProviderSpec, cost as _cost, discover as _discover, health as _health, quota as _quota, usage as _usage

SPEC = ProviderSpec(
    id="codex", display_name="Codex", brand_color="#10A37F", icon_hint="codex",
    windows=("5h", "7d", "session"), source_hint_key="provider.source.codex",
    homes=(HomeSpec("CODEX_HOME", ".codex", ("sessions", "archived_sessions", "auth.json"),
                    ("sessions/**/*.jsonl", "archived_sessions/**/*.jsonl"), ".codex*",
                    ("Library/Application Support/orca/codex-runtime-home/home",)),),
    capabilities=("limits.codex.oauthUsage", "hooks.codexNotify"),
)

def discover(ctx): return _discover(ctx, SPEC)
def usage(ctx, home=None): return _usage(ctx, SPEC, home)
def quota(ctx, home=None): return _quota(ctx, SPEC, home)
def cost(ctx, usage_slice): return _cost(ctx, SPEC, usage_slice)
def health(ctx): return _health(ctx, SPEC)
