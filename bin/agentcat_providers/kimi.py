from ._base import HomeSpec, ProviderSpec, cost as _cost, discover as _discover, health as _health, quota as _quota, usage as _usage

SPEC = ProviderSpec(
    id="kimi", display_name="Kimi", brand_color="#111827", icon_hint="kimi",
    windows=("5h", "7d", "monthly"), source_hint_key="provider.source.kimi",
    homes=(HomeSpec("KIMI_HOME", ".kimi", ("config.toml", "kimi.json", "logs"), ("logs/**/*.jsonl",), ".kimi*"),),
    capabilities=("limits.kimi.oauthUsage",), standard_coverage=True,
)

def discover(ctx): return _discover(ctx, SPEC)
def usage(ctx, home=None): return _usage(ctx, SPEC, home)
def quota(ctx, home=None): return _quota(ctx, SPEC, home)
def cost(ctx, usage_slice): return _cost(ctx, SPEC, usage_slice)
def health(ctx): return _health(ctx, SPEC)
