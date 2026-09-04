from ._base import HomeSpec, ProviderSpec, cost as _cost, discover as _discover, health as _health, quota as _quota, usage as _usage

SPEC = ProviderSpec(
    id="copilot", display_name="GitHub Copilot", brand_color="#6E40C9", icon_hint="copilot",
    windows=(), source_hint_key="provider.source.copilot",
    homes=(HomeSpec("COPILOT_HOME", ".copilot", ("session-state",), ("session-state/**/*.jsonl",), ".copilot*"),),
    capabilities=("usage.copilot.localLogs",), standard_coverage=True,
)

def discover(ctx): return _discover(ctx, SPEC)
def usage(ctx, home=None): return _usage(ctx, SPEC, home)
def quota(ctx, home=None): return _quota(ctx, SPEC, home)
def cost(ctx, usage_slice): return _cost(ctx, SPEC, usage_slice)
def health(ctx): return _health(ctx, SPEC)
