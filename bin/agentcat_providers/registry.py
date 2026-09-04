"""Single registry for provider identity, capabilities, homes, and hooks."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from . import antigravity, claude, codex, copilot, gemini, grok, hermes, jetbrains, kimi, opencode
from ._base import ProviderContext, ProviderSpec


PROVIDER_MODULES = (codex, claude, gemini, antigravity, opencode, copilot, kimi, grok, jetbrains, hermes)
PROVIDER_SPECS: Tuple[ProviderSpec, ...] = tuple(module.SPEC for module in PROVIDER_MODULES)
PROVIDER_MODULES_BY_ID = {module.SPEC.id: module for module in PROVIDER_MODULES}
CONFIG_PROVIDER_IDS = tuple(spec.id for spec in PROVIDER_SPECS)
COVERAGE_PROVIDER_IDS = tuple(spec.id for spec in PROVIDER_SPECS if spec.standard_coverage)

CAPABILITY_ORDER = (
    "snapshot.metadata",
    "connector.contract.v1",
    "connector.autoUpdate",
    "connector.channel",
    "connector.daemon",
    "connector.daemon.status.v1",
    "activity.terminal",
    "activity.memory",
    "activity.runtimeModes",
    "connector.config",
    "desktopApps.localMetadata",
    "events.sqlite",
    "usage.dailyTokens",
    "usage.hourlyTokens",
    "usage.breakdown",
    "usage.projects",
    "usage.coverage",
    "projects.daily",
    "projects.dailyCost",
    "insights.periods",
    "insights.burnRate",
    "insights.autoQuota",
    "insights.recommendation",
    "homes.discovery",
    "providerInstances.v1",
    "quota.scoped.v2",
    "providers.metadata.v1",
    "pricing.feed",
    "usage.opencode.sqlite",
    "usage.antigravity.sqlite",
    "usage.copilot.localLogs",
    "limits.liveCache",
    "limits.errorBackoff",
    "limits.quotaFallbackOn429",
    "limits.codex.oauthUsage",
    "limits.claude.oauthUsage",
    "limits.claude.statusline",
    "limits.claude.statuslineQuotas",
    "limits.gemini.codeAssist",
    "limits.grok.oauthUsage",
    "limits.kimi.oauthUsage",
    "usage.grok.sessions",
    "hooks.codexNotify",
    "hooks.claudeStatusline",
    "hooks.claudeHooks",
    "hooks.geminiTelemetry",
    "usage.jetbrains.localQuota",
    "usage.hermes.stateDb",
    "cost.hermes.actual",
    "activity.windowsProcessScan",
)

_PROVIDER_CAPABILITIES = frozenset(
    capability for spec in PROVIDER_SPECS for capability in spec.capabilities
)
_COMMON_CAPABILITIES = frozenset(CAPABILITY_ORDER) - _PROVIDER_CAPABILITIES


def connector_capabilities() -> Tuple[str, ...]:
    declared = _COMMON_CAPABILITIES | frozenset(
        capability for spec in PROVIDER_SPECS for capability in spec.capabilities
    )
    return tuple(capability for capability in CAPABILITY_ORDER if capability in declared)


def provider_metadata() -> Dict[str, Dict[str, Any]]:
    return {spec.id: spec.metadata() for spec in PROVIDER_SPECS}


def provider_home_specs() -> Dict[str, Dict[str, Any]]:
    return {
        spec.id: spec.homes[0].as_legacy_dict()
        for spec in PROVIDER_SPECS
        if spec.homes
    }


def build_providers(
    ctx: ProviderContext,
    provider_config: Mapping[str, Mapping[str, Any]],
    overrides: Optional[Mapping[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build the ordered providers map with per-plugin failure isolation."""
    resolved_overrides = overrides or {}
    providers: Dict[str, Dict[str, Any]] = {}
    for module in PROVIDER_MODULES:
        provider_id = module.SPEC.id
        try:
            if (provider_config.get(provider_id) or {}).get("enabled") is False:
                payload = ctx.invoke("disabled", module.SPEC)
            elif provider_id in resolved_overrides:
                payload = resolved_overrides[provider_id]
            else:
                payload = module.usage(ctx)
        except Exception as exc:
            payload = ctx.invoke("error", module.SPEC, exc)
        providers[provider_id] = ctx.sanitize(payload)
    return providers


def quota(ctx: ProviderContext, provider_id: str) -> Any:
    return ctx.sanitize(PROVIDER_MODULES_BY_ID[provider_id].quota(ctx))


def cost(ctx: ProviderContext, provider_id: str, usage_slice: Dict[str, Any]) -> Any:
    return ctx.sanitize(PROVIDER_MODULES_BY_ID[provider_id].cost(ctx, usage_slice))


def discover(ctx: ProviderContext, provider_ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    selected = set(provider_ids or CONFIG_PROVIDER_IDS)
    return {
        module.SPEC.id: module.discover(ctx)
        for module in PROVIDER_MODULES
        if module.SPEC.id in selected
    }
