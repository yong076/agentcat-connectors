from ._base import HomeSpec, ProviderSpec, cost as _cost, discover as _discover, health as _health, quota as _quota, usage as _usage

SPEC = ProviderSpec(
    id="antigravity", display_name="Antigravity", brand_color="#7C3AED", icon_hint="antigravity",
    windows=("daily",), source_hint_key="provider.source.antigravity",
    homes=(HomeSpec("ANTIGRAVITY_HOME", ".gemini/antigravity-cli", ("settings.json", "antigravity-oauth-token"),
                    ("*.log", "*.db", "*.sqlite"), ".gemini/antigravity-cli*"),),
    capabilities=("usage.antigravity.sqlite",), standard_coverage=True,
)

def discover(ctx): return _discover(ctx, SPEC)
def usage(ctx, home=None): return _usage(ctx, SPEC, home)
def quota(ctx, home=None): return _quota(ctx, SPEC, home)
def cost(ctx, usage_slice): return _cost(ctx, SPEC, usage_slice)
def health(ctx): return _health(ctx, SPEC)
