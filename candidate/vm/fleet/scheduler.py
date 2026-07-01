from __future__ import annotations
from vm import catalog
from vm.errors import ProviderError
from vm.logging_setup import get_logger

log = get_logger("vm.scheduler")


def known_capacity(provider, gpu: str) -> int | None:
    """Total known available count for gpu, or None if the provider can't report counts
    (Lambda reports region presence but no counts; Nebius has no capacity endpoint)."""
    try:
        slots = provider.capacity(gpu)
    except ProviderError as e:
        log.warning("[%s] capacity query failed: %s", e.provider, e.message)
        return None
    if slots is None:
        return None
    counts = [s.available for s in slots if s.available is not None]
    return sum(counts) if counts else None


def allocate(gpu: str, count: int, providers: list) -> list[tuple[str, int]]:
    """Round-robin even-spread across eligible providers, capped by known capacity.

    README says "spread across providers", so we distribute one VM at a time round-robin
    rather than packing. A provider with a known finite count is capped there; unknown-count
    providers (Lambda, Nebius) are never capped by planning and rely on execution-time
    failover if over-assigned. Returns [(provider_name, n), ...] in provider order.

    The sum is < count only when every eligible provider has a finite known cap and their
    total is below the request — the manager then surfaces the shortfall and rolls back.
    """
    eligible = [p for p in providers if catalog.supports(p.name, gpu)]
    caps = {p.name: known_capacity(p, gpu) for p in eligible}  # int cap, or None = uncapped
    assigned = {p.name: 0 for p in eligible}
    remaining = count
    while remaining > 0:
        progressed = False
        for p in eligible:
            if remaining <= 0:
                break
            cap = caps[p.name]
            if cap is not None and assigned[p.name] >= cap:
                continue  # this provider's known capacity is exhausted
            assigned[p.name] += 1
            remaining -= 1
            progressed = True
        if not progressed:
            break  # all known-capacity providers full and none are uncapped
    return [(name, n) for name, n in assigned.items() if n > 0]
