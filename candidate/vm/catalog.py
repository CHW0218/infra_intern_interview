from __future__ import annotations
from vm.errors import InvalidArgumentError, UnsupportedOperationError

# canonical -> per-provider mapping (None = unsupported by that provider)
_MAP = {
    "a100.1x": {"crusoe": {"type": "a100.1x"}, "lambda": {"instance_type_name": "gpu_1x_a100"}, "nebius": None},
    "a100.8x": {"crusoe": {"type": "a100.8x"}, "lambda": {"instance_type_name": "gpu_8x_a100"}, "nebius": None},
    "h100.1x": {"crusoe": {"type": "h100.1x"}, "lambda": {"instance_type_name": "gpu_1x_h100"},
                "nebius": {"platform": "gpu-h100-sxm", "preset": "1gpu-16vcpu-200gb"}},
    "h100.8x": {"crusoe": {"type": "h100.8x"}, "lambda": {"instance_type_name": "gpu_8x_h100"},
                "nebius": {"platform": "gpu-h100-sxm", "preset": "8gpu-160vcpu-1600gb"}},
    "h200.1x": {"crusoe": None, "lambda": None,
                "nebius": {"platform": "gpu-h200-sxm", "preset": "1gpu-20vcpu-256gb"}},
    "h200.8x": {"crusoe": None, "lambda": None,
                "nebius": {"platform": "gpu-h200-sxm", "preset": "8gpu-160vcpu-2048gb"}},
}

_PRICE = {"a100.1x": 1.48, "a100.8x": 11.84, "h100.1x": 3.46, "h100.8x": 27.68,
          "h200.1x": None, "h200.8x": None}

CANONICAL = list(_MAP.keys())
PROVIDERS = ("crusoe", "lambda", "nebius")


def _entry(gpu: str) -> dict:
    if gpu not in _MAP:
        raise InvalidArgumentError("catalog", f"unknown gpu type '{gpu}' (known: {', '.join(CANONICAL)})")
    return _MAP[gpu]


def supports(provider: str, gpu: str) -> bool:
    return gpu in _MAP and _MAP[gpu].get(provider) is not None


def to_native(provider: str, gpu: str) -> dict:
    entry = _entry(gpu)
    native = entry.get(provider)
    if native is None:
        ok = [g for g in CANONICAL if supports(provider, g)]
        raise UnsupportedOperationError(provider, f"does not support {gpu} (supports: {', '.join(ok)})")
    return dict(native)


def providers_for(gpu: str) -> list[str]:
    _entry(gpu)
    return [p for p in PROVIDERS if supports(p, gpu)]


def price(gpu: str) -> float | None:
    return _PRICE.get(gpu)


# canonical -> gpu_count (used when a provider reports capacity in GPUs)
GPU_COUNT = {"a100.1x": 1, "a100.8x": 8, "h100.1x": 1, "h100.8x": 8, "h200.1x": 1, "h200.8x": 8}

# --- region normalization ---
# canonical region -> per-provider native name (None = provider has no region concept)
_REGION = {
    "us-west": {"crusoe": "us-west1", "lambda": "us-west-1", "nebius": None},
    "us-east": {"crusoe": "us-east1", "lambda": "us-east-1", "nebius": None},
    "eu-west": {"crusoe": "eu-west1", "lambda": "eu-west-1", "nebius": None},
}
REGIONS = list(_REGION.keys())
# reverse index: (provider, native) -> canonical
_REGION_REV = {(p, native): canon
               for canon, m in _REGION.items() for p, native in m.items() if native}


def region_to_native(provider: str, canonical: str | None) -> str | None:
    """Canonical region -> provider-native. None stays None (provider default / no region)."""
    if canonical is None:
        return None
    if canonical not in _REGION:
        raise InvalidArgumentError("catalog", f"unknown region '{canonical}' (known: {', '.join(REGIONS)})")
    return _REGION[canonical].get(provider)


def region_to_canonical(provider: str, native: str | None) -> str | None:
    """Provider-native region -> canonical (for normalizing capacity() output)."""
    if native is None:
        return None
    return _REGION_REV.get((provider, native), native)
