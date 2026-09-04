from ._base import HomeSpec, ProviderSpec, cost as _cost, discover as _discover, health as _health, quota as _quota, usage as _usage

SPEC = ProviderSpec(
    id="grok", display_name="Grok", brand_color="#000000", icon_hint="grok",
    windows=("5h", "7d", "monthly", "media"), source_hint_key="provider.source.grok",
    homes=(HomeSpec("GROK_HOME", ".grok", ("config.json", "sessions"), ("sessions/**/*.jsonl",), ".grok*"),),
    capabilities=("limits.grok.oauthUsage", "usage.grok.sessions"), standard_coverage=True,
)

def discover(ctx): return _discover(ctx, SPEC)
def usage(ctx, home=None): return _usage(ctx, SPEC, home)
def quota(ctx, home=None): return _quota(ctx, SPEC, home)
def cost(ctx, usage_slice): return _cost(ctx, SPEC, usage_slice)
def health(ctx): return _health(ctx, SPEC)
