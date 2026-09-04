from ._base import HomeSpec, ProviderSpec, cost as _cost, discover as _discover, health as _health, quota as _quota, usage as _usage

SPEC = ProviderSpec(
    id="opencode", display_name="OpenCode", brand_color="#111827", icon_hint="opencode",
    windows=(), source_hint_key="provider.source.opencode",
    homes=(HomeSpec("OPENCODE_HOME", ".local/share/opencode", ("opencode.db", "storage"),
                    ("opencode.db", "storage/**/*.json")),),
    capabilities=("usage.opencode.sqlite",), standard_coverage=True,
)

def discover(ctx): return _discover(ctx, SPEC)
def usage(ctx, home=None): return _usage(ctx, SPEC, home)
def quota(ctx, home=None): return _quota(ctx, SPEC, home)
def cost(ctx, usage_slice): return _cost(ctx, SPEC, usage_slice)
def health(ctx): return _health(ctx, SPEC)
