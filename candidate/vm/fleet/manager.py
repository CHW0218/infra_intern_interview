from __future__ import annotations
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from vm import catalog
from vm.errors import (CapacityError, ReservedInstanceError, NotFoundError,
                       FleetUnfulfilledError, ProviderError)
from vm.fleet import scheduler
from vm.fleet.store import Store, FleetRecord
from vm.logging_setup import get_logger
from vm.providers import registry

log = get_logger("vm.fleet")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FleetManager:
    def __init__(self, store: Store | None = None, providers: list | None = None):
        self.store = store or Store()
        self._providers = providers if providers is not None else registry.all()
        self._by_name = {p.name: p for p in self._providers}

    def _run_round(self, gpu, provider_names, name, index_start):
        """Create one VM per entry in provider_names, ALL concurrently (across providers).
        Each create uses count=1 (so Lambda partial-fills instead of failing atomically).
        Returns (successes: list[Instance], exhausted: set[provider_name])."""
        successes, exhausted = [], set()
        if not provider_names:
            return successes, exhausted
        with ThreadPoolExecutor(max_workers=min(len(provider_names), 16)) as pool:
            futs = {}
            for i, pname in enumerate(provider_names):
                vm_name = f"{name}-{pname}-{index_start + i}"
                futs[pool.submit(self._by_name[pname].create, gpu, 1, vm_name)] = pname
            for f in as_completed(futs):
                pname = futs[f]
                try:
                    res = f.result()
                except ProviderError as e:  # e.g. UnsupportedOperationError raised pre-network
                    log.error("[%s] create failed: %s", e.provider, e.message)
                    exhausted.add(pname)
                    continue
                successes += res.successes
                for e in res.errors:
                    log.error("[%s] create failed: %s", e.provider, e.message)
                    if isinstance(e, CapacityError):
                        exhausted.add(pname)
        return successes, exhausted

    def create(self, gpu: str, count: int, name: str) -> FleetRecord:
        eligible = [p for p in self._providers if catalog.supports(p.name, gpu)]
        if not eligible:
            raise FleetUnfulfilledError(f"no provider supports {gpu}", 0, [])
        plan = scheduler.allocate(gpu, count, self._providers)
        log.info("fleet '%s' plan: %s", name, plan)

        # Round 1: fire every provider's allocation concurrently in one shot (cross-provider).
        round1 = [pname for pname, n in plan for _ in range(n)]
        provisioned, exhausted = self._run_round(gpu, round1, name, 0)
        idx = len(round1)

        # Failover rounds: redistribute only the shortfall to providers not yet exhausted.
        while len(provisioned) < count:
            candidates = [p.name for p in eligible if p.name not in exhausted]
            if not candidates:
                break
            shortfall = count - len(provisioned)
            tasks = [candidates[i % len(candidates)] for i in range(shortfall)]
            log.warning("fleet '%s' failover: %d short, retrying on %s", name, shortfall, candidates)
            made, more_exhausted = self._run_round(gpu, tasks, name, idx)
            idx += len(tasks)
            provisioned += made
            exhausted |= more_exhausted
            if not made:
                break  # no progress this round → stop (avoid spinning)

        if len(provisioned) < count:
            log.error("fleet '%s' unfulfilled (%d/%d) — rolling back", name, len(provisioned), count)
            undestroyable = self._rollback(provisioned)
            raise FleetUnfulfilledError(
                f"could only provision {len(provisioned)}/{count} {gpu}",
                rolled_back=len(provisioned) - len(undestroyable), undestroyable=undestroyable)

        rec = FleetRecord(
            name=name, gpu=gpu, created_at=_now(), status="active",
            vms=[{"provider": i.provider, "id": i.id, "region": i.region} for i in provisioned])
        self.store.save(rec)
        return rec

    def _rollback(self, instances) -> list[str]:
        """Destroy provisioned instances concurrently; return ids that couldn't be destroyed."""
        undestroyable = []
        if not instances:
            return undestroyable
        with ThreadPoolExecutor(max_workers=min(len(instances), 16)) as pool:
            fut = {pool.submit(self._by_name[i.provider].destroy, i.id): i for i in instances}
            for f in as_completed(fut):
                inst = fut[f]
                try:
                    f.result()
                except ReservedInstanceError as e:
                    log.warning("[%s] cannot destroy %s (reserved): %s", inst.provider, inst.id, e.message)
                    undestroyable.append(inst.id)
                except NotFoundError:
                    pass
                except ProviderError as e:
                    log.error("[%s] rollback destroy failed for %s: %s", inst.provider, inst.id, e.message)
                    undestroyable.append(inst.id)
        return undestroyable

    def list(self) -> list[FleetRecord]:
        return self.store.list()

    def status(self, name: str) -> dict:
        rec = self.store.get(name)
        if rec is None:
            raise ValueError(f"unknown fleet '{name}'")

        def probe(v):
            try:
                inst = self._by_name[v["provider"]].get(v["id"])
                return {**v, "state": inst.state.value}
            except NotFoundError:
                return {**v, "state": "MISSING"}  # deleted out-of-band → drift
            except ProviderError as e:
                log.error("[%s] status probe failed for %s: %s", v["provider"], v["id"], e.message)
                return {**v, "state": "ERROR"}

        with ThreadPoolExecutor(max_workers=max(1, len(rec.vms))) as pool:
            vms = list(pool.map(probe, rec.vms))
        return {"name": rec.name, "gpu": rec.gpu, "status": rec.status,
                "created_at": rec.created_at, "vms": vms}

    def destroy(self, name: str) -> dict:
        rec = self.store.get(name)
        if rec is None:
            raise ValueError(f"unknown fleet '{name}'")

        class _I:  # minimal shim so _rollback can reuse its logic
            def __init__(self, provider, id):
                self.provider, self.id = provider, id
        left = self._rollback([_I(v["provider"], v["id"]) for v in rec.vms])
        self.store.delete(name)
        return {"destroyed": len(rec.vms) - len(left), "left_reserved": left}
