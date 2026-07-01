from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path


@dataclass
class FleetRecord:
    name: str
    gpu: str
    created_at: str
    status: str
    vms: list = field(default_factory=list)


class Store:
    def __init__(self, path=None):
        self._path = Path(path) if path else Path.home() / ".vmfleet" / "fleets.json"

    def _load(self) -> dict:
        if not self._path.exists():
            return {"fleets": {}}
        return json.loads(self._path.read_text())

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self._path)

    def save(self, record: FleetRecord) -> None:
        data = self._load()
        data["fleets"][record.name] = asdict(record)
        self._write(data)

    def get(self, name: str) -> FleetRecord | None:
        d = self._load()["fleets"].get(name)
        return FleetRecord(**d) if d else None

    def list(self) -> list[FleetRecord]:
        return [FleetRecord(**d) for d in self._load()["fleets"].values()]

    def delete(self, name: str) -> None:
        data = self._load()
        data["fleets"].pop(name, None)
        self._write(data)
