from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    CREATING = "CREATING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    DELETING = "DELETING"
    TERMINATED = "TERMINATED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass
class Instance:
    id: str
    name: str
    provider: str
    gpu_type: str
    region: str | None
    state: State
    public_ip: str | None
    private_ip: str | None
    reserved: bool
    reservation_id: str | None
    price_per_hour: float | None


@dataclass
class CapacitySlot:
    provider: str
    gpu_type: str
    region: str | None
    available: int | None  # None = provider cannot report capacity


@dataclass
class CreateResult:
    requested: int
    successes: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def fulfilled(self) -> int:
        return len(self.successes)
