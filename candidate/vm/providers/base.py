from __future__ import annotations
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from vm.models import Instance, CapacitySlot, CreateResult


@dataclass(frozen=True)
class Capabilities:
    supports_stop_start: bool
    native_batch: bool
    queryable_capacity: bool


class Provider(ABC):
    name: str
    capabilities: Capabilities

    @abstractmethod
    def list(self) -> list[Instance]: ...

    @abstractmethod
    def get(self, instance_id: str) -> Instance: ...

    @abstractmethod
    def create(self, gpu: str, count: int, name: str | None = None,
               region: str | None = None, wait: bool = True) -> CreateResult: ...

    @abstractmethod
    def stop(self, instance_id: str) -> Instance: ...

    @abstractmethod
    def start(self, instance_id: str) -> Instance: ...

    @abstractmethod
    def destroy(self, instance_id: str) -> None: ...

    @abstractmethod
    def capacity(self, gpu: str) -> list[CapacitySlot] | None: ...

    def _await(self, check, timeout: float = 25.0, interval: float = 0.4):
        """Poll check() until it returns a non-None value; else raise TimeoutError."""
        deadline = time.monotonic() + timeout
        while True:
            result = check()
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError(f"[{self.name}] timed out after {timeout}s")
            time.sleep(interval)
