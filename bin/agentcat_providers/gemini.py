from ._base import HomeSpec, ProviderSpec, cost as _cost, discover as _discover, health as _health, quota as _quota, usage as _usage

SPEC = ProviderSpec(
    id="gemini", display_name="Gemini", brand_color="#4285F4", icon_hint="gemini",
    windows=("daily",), source_hint_key="provider.source.gemini",
    homes=(HomeSpec("GEMINI_CLI_HOME", ".gemini", ("settings.json", "oauth_creds.json"),
                    ("tmp/**/*.json",), ".gemini*"),),
    capabilities=("limits.gemini.codeAssist", "hooks.geminiTelemetry"), standard_coverage=True,
)

def discover(ctx): return _discover(ctx, SPEC)
def usage(ctx, home=None): return _usage(ctx, SPEC, home)
def quota(ctx, home=None): return _quota(ctx, SPEC, home)
def cost(ctx, usage_slice): return _cost(ctx, SPEC, usage_slice)
def health(ctx): return _health(ctx, SPEC)
