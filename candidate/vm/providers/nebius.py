from __future__ import annotations
import sys

import grpc

from vm import catalog, config
from vm.errors import (AuthError, NotFoundError, CapacityError, InvalidArgumentError,
                       PreconditionError, ProviderError)
from vm.models import Instance, CapacitySlot, CreateResult, State
from vm.providers.base import Provider, Capabilities

if config.GENERATED_PATH not in sys.path:
    sys.path.insert(0, config.GENERATED_PATH)
from nebius.compute.v1 import instance_pb2, instance_service_pb2, instance_service_pb2_grpc  # noqa: E402

_STATE = {1: State.CREATING, 2: State.RUNNING, 3: State.STOPPING, 4: State.STOPPED,
          5: State.STARTING, 6: State.DELETING, 7: State.ERROR}

_ERR = {grpc.StatusCode.UNAUTHENTICATED: AuthError, grpc.StatusCode.PERMISSION_DENIED: AuthError,
        grpc.StatusCode.NOT_FOUND: NotFoundError, grpc.StatusCode.RESOURCE_EXHAUSTED: CapacityError,
        grpc.StatusCode.INVALID_ARGUMENT: InvalidArgumentError,
        grpc.StatusCode.FAILED_PRECONDITION: PreconditionError}


class NebiusProvider(Provider):
    name = "nebius"
    capabilities = Capabilities(supports_stop_start=True, native_batch=False, queryable_capacity=False)

    def __init__(self, api_key: str | None = None):
        self._cfg = config.NEBIUS
        self._parent = self._cfg.scope
        key = api_key or self._cfg.api_key
        self._meta = [("authorization", f"Bearer {key}")]
        self._channel = grpc.insecure_channel(self._cfg.base_url)
        self._stub = instance_service_pb2_grpc.InstanceServiceStub(self._channel)

    def _translate(self, e: grpc.RpcError) -> ProviderError:
        return _ERR.get(e.code(), ProviderError)(self.name, e.details() or str(e.code()))

    def _normalize(self, inst) -> Instance:
        platform, preset = inst.spec.resources.platform, inst.spec.resources.preset
        canon = next((c for c in catalog.CANONICAL
                      if catalog.supports("nebius", c)
                      and catalog.to_native("nebius", c) == {"platform": platform, "preset": preset}),
                     f"{platform}/{preset}")
        nets = inst.status.network_interfaces
        return Instance(
            id=inst.metadata.id, name=inst.metadata.name, provider=self.name, gpu_type=canon,
            region=None, state=_STATE.get(inst.status.state, State.UNKNOWN),
            public_ip=(nets[0].public_ip_address if nets else None),
            private_ip=(nets[0].ip_address if nets else None),
            reserved=bool(inst.status.reservation_id), reservation_id=inst.status.reservation_id or None,
            price_per_hour=catalog.price(canon))

    def list(self) -> list[Instance]:
        try:
            resp = self._stub.List(
                instance_service_pb2.ListInstancesRequest(parent_id=self._parent), metadata=self._meta)
        except grpc.RpcError as e:
            raise self._translate(e)
        return [self._normalize(i) for i in resp.instances]

    def get(self, instance_id: str) -> Instance:
        try:
            inst = self._stub.Get(instance_service_pb2.GetInstanceRequest(id=instance_id), metadata=self._meta)
        except grpc.RpcError as e:
            raise self._translate(e)
        return self._normalize(inst)

    def _await_state(self, instance_id: str, targets: set[State]) -> Instance:
        def check():
            inst = self.get(instance_id)
            return inst if inst.state in targets else None
        return self._await(check)

    def create(self, gpu, count=1, name=None, region=None, wait=True) -> CreateResult:
        native = catalog.to_native(self.name, gpu)
        res = CreateResult(requested=count)
        for i in range(count):
            vm_name = f"{name or gpu}-{i}" if count > 1 else (name or gpu)
            req = instance_service_pb2.CreateInstanceRequest(
                metadata=instance_pb2.ResourceMetadata(parent_id=self._parent, name=vm_name),
                spec=instance_pb2.InstanceSpec(
                    resources=instance_pb2.ResourcesSpec(platform=native["platform"], preset=native["preset"]),
                    reservation_policy=instance_pb2.ReservationPolicy(policy=0)))  # AUTO
            try:
                op = self._stub.Create(req, metadata=self._meta)
            except grpc.RpcError as e:
                res.errors.append(self._translate(e))
                continue
            res.successes.append(self._await_state(op.resource_id, {State.RUNNING})
                                 if wait else self.get(op.resource_id))
        return res

    def stop(self, instance_id: str) -> Instance:
        try:
            self._stub.Stop(instance_service_pb2.StopInstanceRequest(id=instance_id), metadata=self._meta)
        except grpc.RpcError as e:
            raise self._translate(e)
        return self._await_state(instance_id, {State.STOPPED})

    def start(self, instance_id: str) -> Instance:
        try:
            self._stub.Start(instance_service_pb2.StartInstanceRequest(id=instance_id), metadata=self._meta)
        except grpc.RpcError as e:
            raise self._translate(e)
        return self._await_state(instance_id, {State.RUNNING})

    def destroy(self, instance_id: str) -> None:
        try:
            self._stub.Delete(instance_service_pb2.DeleteInstanceRequest(id=instance_id), metadata=self._meta)
        except grpc.RpcError as e:
            raise self._translate(e)

        def gone():
            try:
                self.get(instance_id)
                return None
            except NotFoundError:
                return True
        self._await(gone)

    def capacity(self, gpu: str) -> list[CapacitySlot] | None:
        catalog.to_native(self.name, gpu)  # validates support
        return None  # Nebius exposes no capacity endpoint
