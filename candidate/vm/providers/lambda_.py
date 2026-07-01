from __future__ import annotations
import httpx

from vm import catalog, config
from vm.errors import (AuthError, NotFoundError, CapacityError, ReservedInstanceError,
                       InvalidArgumentError, UnsupportedOperationError, ProviderError)
from vm.models import Instance, CapacitySlot, CreateResult, State
from vm.providers.base import Provider, Capabilities

_STATE = {"active": State.RUNNING, "booting": State.CREATING, "unhealthy": State.ERROR,
          "terminating": State.DELETING, "terminated": State.TERMINATED}


class LambdaProvider(Provider):
    name = "lambda"
    capabilities = Capabilities(supports_stop_start=False, native_batch=True, queryable_capacity=False)

    def __init__(self):
        self._cfg = config.LAMBDA
        self._http = httpx.Client(
            base_url=self._cfg.base_url,
            headers={"Authorization": f"Bearer {self._cfg.api_key}"}, timeout=10.0)

    # --- error translation ---
    def _raise(self, resp: httpx.Response):
        try:
            err = resp.json()["detail"]["error"]
            code, msg = err.get("code", ""), err.get("message", resp.text)
        except Exception:
            code, msg = "", resp.text
        if resp.status_code == 401:
            raise AuthError(self.name, msg or "invalid api key")
        if "reserved-instance" in code:
            raise ReservedInstanceError(self.name, msg)
        if "insufficient-capacity" in code:
            raise CapacityError(self.name, msg)
        if "object-does-not-exist" in code or resp.status_code == 404:
            raise NotFoundError(self.name, msg)
        if "invalid" in code or resp.status_code == 400:
            raise InvalidArgumentError(self.name, msg)
        raise ProviderError(self.name, msg or f"HTTP {resp.status_code}")

    def _normalize(self, d: dict) -> Instance:
        it = d.get("instance_type", {})
        gpus = it.get("specs", {}).get("gpus")
        gpu_name = it.get("name", "")
        canon = next((c for c in catalog.CANONICAL
                      if catalog.supports("lambda", c)
                      and catalog.to_native("lambda", c)["instance_type_name"] == gpu_name), gpu_name)
        native_region = (d.get("region") or {}).get("name")
        return Instance(
            id=d["id"], name=d.get("name") or "", provider=self.name, gpu_type=canon,
            region=catalog.region_to_canonical(self.name, native_region),
            state=_STATE.get(d.get("status", ""), State.UNKNOWN),
            public_ip=d.get("ip"), private_ip=d.get("private_ip"),
            reserved=bool(d.get("is_reserved")), reservation_id=d.get("reservation_id"),
            price_per_hour=(it.get("price_cents_per_hour") or 0) / 100 or catalog.price(canon))

    # --- operations ---
    def list(self) -> list[Instance]:
        r = self._http.get("/api/v1/instances")
        if r.status_code >= 400:
            self._raise(r)
        return [self._normalize(d) for d in r.json()["data"]]

    def get(self, instance_id: str) -> Instance:
        r = self._http.get(f"/api/v1/instances/{instance_id}")
        if r.status_code >= 400:
            self._raise(r)
        return self._normalize(r.json()["data"])

    def _regions_to_try(self, gpu: str) -> list[str]:
        """Native regions to attempt when region is unspecified: default first, then any
        region reported with capacity. Lets a create gather capacity across regions."""
        default_native = catalog.region_to_native(self.name, self._cfg.default_region)
        order = [default_native] if default_native else []
        for s in (self.capacity(gpu) or []):
            native = catalog.region_to_native(self.name, s.region)
            if native and native not in order:
                order.append(native)
        return order or [default_native]

    def create(self, gpu, count=1, name=None, region=None, wait=True) -> CreateResult:
        native_type = catalog.to_native(self.name, gpu)["instance_type_name"]
        res = CreateResult(requested=count)
        # Explicit region → honor it only; unspecified → auto-select / iterate regions.
        # Note: Lambda launch is atomic all-or-nothing per region (fleet path uses count=1).
        if region is not None:
            regions = [catalog.region_to_native(self.name, region)]
        else:
            regions = self._regions_to_try(gpu)
        last_err = None
        for native_region in regions:
            body = {"region_name": native_region, "instance_type_name": native_type,
                    "ssh_key_names": [self._cfg.ssh_key], "name": name or gpu, "quantity": count}
            r = self._http.post("/api/v1/instance-operations/launch", json=body)
            if r.status_code >= 400:
                try:
                    self._raise(r)
                except CapacityError as e:
                    last_err = e
                    continue  # try next region
                except ProviderError as e:
                    res.errors.append(e)
                    return res
            for iid in r.json()["data"]["instance_ids"]:
                res.successes.append(self.get(iid))  # already active (synchronous)
            return res
        if last_err:
            res.errors.append(last_err)
        return res

    def stop(self, instance_id: str) -> Instance:
        raise UnsupportedOperationError(self.name, "Lambda has no stop operation")

    def start(self, instance_id: str) -> Instance:
        raise UnsupportedOperationError(self.name, "Lambda has no start operation")

    def destroy(self, instance_id: str) -> None:
        r = self._http.post("/api/v1/instance-operations/terminate",
                            json={"instance_ids": [instance_id]})
        if r.status_code >= 400:
            self._raise(r)

    def capacity(self, gpu: str) -> list[CapacitySlot] | None:
        native = catalog.to_native(self.name, gpu)
        r = self._http.get("/api/v1/instance-types")
        if r.status_code >= 400:
            self._raise(r)
        entry = r.json()["data"].get(native["instance_type_name"], {})
        regions = entry.get("regions_with_capacity_available", [])
        # region-level presence only (no counts) → available=None; report canonical regions
        return [CapacitySlot(self.name, gpu,
                             catalog.region_to_canonical(self.name, reg["name"]), None)
                for reg in regions]
