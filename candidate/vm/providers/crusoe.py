from __future__ import annotations
import httpx

from vm import catalog, config
from vm.errors import (AuthError, NotFoundError, CapacityError, InvalidArgumentError,
                       PreconditionError, ProviderError)
from vm.models import Instance, CapacitySlot, CreateResult, State
from vm.providers.base import Provider, Capabilities

_STATE = {
    "STATE_CREATING": State.CREATING, "STATE_RUNNING": State.RUNNING,
    "STATE_STOPPING": State.STOPPING, "STATE_STOPPED": State.STOPPED,
    "STATE_STARTING": State.STARTING, "STATE_DELETING": State.DELETING,
}


class CrusoeProvider(Provider):
    name = "crusoe"
    capabilities = Capabilities(supports_stop_start=True, native_batch=False, queryable_capacity=True)

    def __init__(self):
        self._cfg = config.CRUSOE
        self._base = f"/v1alpha5/projects/{self._cfg.scope}"
        self._http = httpx.Client(
            base_url=self._cfg.base_url,
            headers={"Authorization": f"Bearer {self._cfg.api_key}"}, timeout=10.0)

    def _raise(self, resp: httpx.Response):
        try:
            detail = resp.json()["detail"]
            code, msg = detail.get("code", ""), detail.get("message", resp.text)
        except Exception:
            code, msg = "", resp.text
        mapping = {"UNAUTHENTICATED": AuthError, "PERMISSION_DENIED": AuthError,
                   "NOT_FOUND": NotFoundError, "RESOURCE_EXHAUSTED": CapacityError,
                   "INVALID_ARGUMENT": InvalidArgumentError, "FAILED_PRECONDITION": PreconditionError}
        raise mapping.get(code, ProviderError)(self.name, msg or f"HTTP {resp.status_code}")

    def _normalize(self, d: dict) -> Instance:
        return Instance(
            id=d["id"], name=d.get("name") or "", provider=self.name, gpu_type=d.get("type", ""),
            region=catalog.region_to_canonical(self.name, d.get("location")),
            state=_STATE.get(d.get("state", ""), State.UNKNOWN),
            public_ip=d.get("ip_address"), private_ip=d.get("private_ip_address"),
            reserved=(d.get("billing_type") == "reserved"), reservation_id=d.get("reservation_id"),
            price_per_hour=catalog.price(d.get("type", "")))

    def list(self) -> list[Instance]:
        r = self._http.get(f"{self._base}/compute/vms/instances")
        if r.status_code >= 400:
            self._raise(r)
        return [self._normalize(d) for d in r.json()["items"]]

    def get(self, instance_id: str) -> Instance:
        r = self._http.get(f"{self._base}/compute/vms/instances/{instance_id}")
        if r.status_code >= 400:
            self._raise(r)
        return self._normalize(r.json())

    def _await_state(self, instance_id: str, targets: set[State]) -> Instance:
        def check():
            inst = self.get(instance_id)
            return inst if inst.state in targets else None
        return self._await(check)

    def _regions_to_try(self, gpu: str) -> list[str]:
        """Native regions ordered: default first, then others by known capacity desc.
        Lets one provider gather its full cross-region capacity (e.g. reserved nodes in a
        single region) instead of stranding it behind a fixed default."""
        default_native = catalog.region_to_native(self.name, self._cfg.default_region)
        slots = sorted(self.capacity(gpu) or [],
                       key=lambda s: (s.region != self._cfg.default_region, -(s.available or 0)))
        natives = []
        for s in slots:
            if (s.available or 0) <= 0:
                continue
            native = catalog.region_to_native(self.name, s.region)
            if native and native not in natives:
                natives.append(native)
        if default_native and default_native not in natives:
            natives.insert(0, default_native)
        return natives or [default_native]

    def _create_one(self, native_type: str, vm_name: str, regions: list[str], wait: bool) -> Instance:
        last_err = None
        for native_region in regions:
            body = {"name": vm_name, "type": native_type, "location": native_region,
                    "ssh_key": self._cfg.ssh_key}
            r = self._http.post(f"{self._base}/compute/vms/instances", json=body)
            if r.status_code >= 400:
                try:
                    self._raise(r)
                except CapacityError as e:
                    last_err = e
                    continue  # try next region
            else:
                iid = r.json()["instance"]["id"]
                return self._await_state(iid, {State.RUNNING}) if wait else self.get(iid)
        # exhausted all regions with CapacityError (or, defensively, empty region list)
        raise last_err or CapacityError(self.name, f"no capacity for {native_type} in any region")

    def create(self, gpu, count=1, name=None, region=None, wait=True) -> CreateResult:
        native_type = catalog.to_native(self.name, gpu)["type"]
        if region is not None:
            regions = [catalog.region_to_native(self.name, region)]
        else:
            regions = self._regions_to_try(gpu)
        res = CreateResult(requested=count)
        for i in range(count):
            vm_name = f"{name or gpu}-{i}" if count > 1 else (name or gpu)
            try:
                res.successes.append(self._create_one(native_type, vm_name, regions, wait))
            except ProviderError as e:
                res.errors.append(e)
        return res

    def stop(self, instance_id: str) -> Instance:
        r = self._http.patch(f"{self._base}/compute/vms/instances/{instance_id}",
                            json={"action": "STOP"})
        if r.status_code >= 400:
            self._raise(r)
        return self._await_state(instance_id, {State.STOPPED})

    def start(self, instance_id: str) -> Instance:
        r = self._http.patch(f"{self._base}/compute/vms/instances/{instance_id}",
                            json={"action": "START"})
        if r.status_code >= 400:
            self._raise(r)
        return self._await_state(instance_id, {State.RUNNING})

    def destroy(self, instance_id: str) -> None:
        r = self._http.request("DELETE", f"{self._base}/compute/vms/instances/{instance_id}")
        if r.status_code >= 400:
            self._raise(r)

        def gone():
            try:
                self.get(instance_id)
                return None
            except NotFoundError:
                return True
        self._await(gone)

    def capacity(self, gpu: str) -> list[CapacitySlot] | None:
        native = catalog.to_native(self.name, gpu)
        r = self._http.get(f"{self._base}/capacity")
        if r.status_code >= 400:
            self._raise(r)
        return [CapacitySlot(self.name, gpu,
                             catalog.region_to_canonical(self.name, c["location"]),
                             c["total_available"])
                for c in r.json()["items"] if c["vm_type"] == native["type"]]
