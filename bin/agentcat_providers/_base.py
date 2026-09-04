"""Provider plugin contracts shared by the single-file connector runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple


@dataclass(frozen=True)
class HomeSpec:
    env: Optional[str]
    default: str
    markers: Tuple[str, ...]
    usage_globs: Tuple[str, ...] = ()
    sibling_glob: Optional[str] = None
    known_runtime_homes: Tuple[str, ...] = ()

    def as_legacy_dict(self) -> Dict[str, Any]:
        return {
            "envVar": self.env,
            "defaultName": self.default,
            "siblingGlob": self.sibling_glob,
            "usageGlobs": self.usage_globs,
            "markers": self.markers,
            "knownRuntimeHomes": self.known_runtime_homes,
        }


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    display_name: str
    brand_color: str
    icon_hint: str
    windows: Tuple[str, ...]
    source_hint_key: str
    homes: Tuple[HomeSpec, ...]
    capabilities: Tuple[str, ...] = ()
    standard_coverage: bool = False

    def metadata(self) -> Dict[str, Any]:
        return {
            "displayName": self.display_name,
            "brandColor": self.brand_color,
            "iconHint": self.icon_hint,
            "windows": list(self.windows),
            "sourceHintKey": self.source_hint_key,
        }


@dataclass
class ProviderContext:
    callbacks: Dict[str, Callable[..., Any]] = field(default_factory=dict)

    def invoke(self, hook: str, spec: ProviderSpec, *args: Any) -> Any:
        callback = self.callbacks.get(hook)
        if callback is None:
            return {} if hook in {"cost", "health"} else [] if hook == "discover" else None
        return callback(spec, *args)

    def sanitize(self, payload: Any) -> Dict[str, Any]:
        callback = self.callbacks.get("sanitize")
        sanitized = callback(payload) if callback is not None else payload
        return sanitized if isinstance(sanitized, dict) else {"status": "error", "error": "invalid provider payload"}


def discover(ctx: ProviderContext, spec: ProviderSpec) -> Any:
    return ctx.invoke("discover", spec)


def usage(ctx: ProviderContext, spec: ProviderSpec, home: Any = None) -> Any:
    return ctx.invoke("usage", spec, home)


def quota(ctx: ProviderContext, spec: ProviderSpec, home: Any = None) -> Any:
    return ctx.invoke("quota", spec, home)


def cost(ctx: ProviderContext, spec: ProviderSpec, usage_slice: Any) -> Any:
    return ctx.invoke("cost", spec, usage_slice)


def health(ctx: ProviderContext, spec: ProviderSpec) -> Any:
    return ctx.invoke("health", spec)
