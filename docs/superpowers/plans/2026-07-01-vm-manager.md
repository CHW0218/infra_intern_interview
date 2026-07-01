# Unified GPU VM Manager — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python CLI (`vm`) that manages GPU VMs across Crusoe (REST/async), Lambda (REST/sync), and Nebius (gRPC/async) behind one clean abstraction, plus a fleet manager that schedules across providers with failover and rollback.

**Architecture:** Adapter pattern — one abstract `Provider` interface, three concrete adapters translating to each protocol and normalizing responses. Async completion is judged by polling observed resource state (the only signal Nebius exposes). Fleet layer attempts creates concurrently, fails over on capacity errors, and rolls back on unfulfillable requests.

**Tech Stack:** Python 3.11+, `httpx` (REST), `grpcio` + pre-generated stubs in `mock_servers/generated/` (gRPC), `argparse` (CLI), `pytest` (tests). Standard-library `concurrent.futures.ThreadPoolExecutor` for concurrency, `logging` for provider-tagged logs.

## Global Constraints

- All solution code lives under `candidate/` (gitignored except `.gitkeep`); tests under `candidate/tests/`.
- Package name: `vm`. Console entry: `python -m vm ...` (and a `vm` script via console_scripts).
- Every normalized error subclasses `ProviderError` and carries a `provider: str`. CLI prints `Error [<provider>]: <msg>` to stderr with exit code 1 — never a raw traceback.
- Canonical GPU vocabulary: `a100.1x, a100.8x, h100.1x, h100.8x, h200.1x, h200.8x`.
- Canonical region vocabulary: `us-west, us-east, eu-west` (Crusoe `us-westN`/Lambda `us-west-N`/Nebius none). `capacity()` reports canonical regions; `--region` accepts canonical; region selection is provider-internal (auto-select + iterate on `CapacityError`).
- Python target: works on 3.9+ (every module using `X | Y` annotations begins with `from __future__ import annotations`). Prefer a 3.11 venv if available.
- Provider endpoints/keys/scope (from README): Crusoe `http://localhost:8001` / `crusoe-test-key-001` / project `proj-001`; Lambda `http://localhost:8002` / `lambda-test-key-001`; Nebius `localhost:50051` / `nebius-test-key-001` / parent `project-e1a2b3c4`.
- Default ssh key `default-key`; default region: Crusoe `us-west1`, Lambda `us-west-1`, Nebius none.
- Nebius stubs are imported by adding `<repo>/mock_servers/generated` to `sys.path` (computed from repo root); no proto compilation.
- FastAPI wraps `HTTPException` detail under a top-level `"detail"` key — Crusoe/Lambda error parsing must look under `resp.json()["detail"]`.

---

## File Structure

```
candidate/
├── pyproject.toml               # package + console entry + pytest config
├── README.md                    # how to run
├── vm/
│   ├── __init__.py
│   ├── __main__.py              # `python -m vm` → cli.main()
│   ├── models.py                # State, Instance, CapacitySlot, CreateResult
│   ├── errors.py                # ProviderError hierarchy (each carries provider)
│   ├── logging_setup.py         # configure logging; provider-tagged helpers
│   ├── config.py                # endpoints, keys, scope, defaults, generated-path
│   ├── catalog.py               # canonical GPU ↔ per-provider naming + support matrix
│   ├── output.py                # table + json rendering
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py              # Provider ABC, Capabilities, _await helper
│   │   ├── lambda_.py           # LambdaProvider (sync REST)
│   │   ├── crusoe.py            # CrusoeProvider (async REST, project-scoped)
│   │   ├── nebius.py            # NebiusProvider (gRPC)
│   │   └── registry.py          # name → provider factory
│   ├── fleet/
│   │   ├── __init__.py
│   │   ├── store.py             # JSON persistence (~/.vmfleet/fleets.json)
│   │   ├── scheduler.py         # capacity query + allocation (S1)
│   │   └── manager.py           # orchestration: create/list/status/destroy
│   └── cli.py                   # argparse surface + top-level error handling
└── tests/
    ├── conftest.py              # per-provider session fixtures (start server if not running)
    ├── test_catalog.py          # unit: GPU + region mapping, support matrix
    ├── test_errors.py           # unit
    ├── test_store.py            # unit
    ├── test_scheduler.py        # unit: allocation with fakes
    ├── test_cli_smoke.py        # unit: arg parsing
    ├── test_lambda.py           # integration (lambda_server fixture)
    ├── test_crusoe.py           # integration (crusoe_server fixture)
    ├── test_nebius.py           # integration (nebius_server fixture)
    └── test_fleet_integration.py   # integration (all_servers fixture)
```

Per-provider test files + fixtures keep the integration suites **independent**, so the three
provider builds can be implemented and tested in parallel (each touches only its own file and
its own mock server on a distinct port).

---

## Task 1: Project scaffolding

**Files:**
- Create: `candidate/pyproject.toml`, `candidate/vm/__init__.py`, `candidate/vm/__main__.py`, `candidate/tests/__init__.py`

**Interfaces:**
- Produces: importable `vm` package; `python -m vm` runs `vm.cli.main` (stubbed until Task 9).

- [ ] **Step 1: Create `candidate/pyproject.toml`**

```toml
[project]
name = "vm-manager"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["httpx>=0.27", "grpcio>=1.60", "protobuf>=4.25"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
vm = "vm.cli:main"

[tool.pytest.ini_options]
markers = ["integration: requires live mock servers"]
addopts = "-ra"
```

- [ ] **Step 2: Create package files**

`candidate/vm/__init__.py`: empty.
`candidate/tests/__init__.py`: empty.

`candidate/vm/__main__.py`:
```python
from vm.cli import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify import**

Run: `cd candidate && python -c "import vm; print('ok')"`
Expected: `ok` (cli import will be added Task 9; `__main__` not executed here).

- [ ] **Step 4: Commit**

```bash
git add candidate/pyproject.toml candidate/vm candidate/tests
git commit -m "chore: scaffold vm package"
```

---

## Task 2: Normalized model + errors + logging

**Files:**
- Create: `candidate/vm/models.py`, `candidate/vm/errors.py`, `candidate/vm/logging_setup.py`
- Test: `candidate/tests/test_errors.py`

**Interfaces:**
- Produces:
  - `State` (Enum): `CREATING, RUNNING, STOPPING, STOPPED, STARTING, DELETING, TERMINATED, ERROR, UNKNOWN`.
  - `Instance` dataclass: `id, name, provider, gpu_type, region, state:State, public_ip, private_ip, reserved:bool, reservation_id, price_per_hour`.
  - `CapacitySlot` dataclass: `provider, gpu_type, region, available:int|None`.
  - `CreateResult` dataclass: `requested:int, successes:list[Instance], errors:list[ProviderError]`; property `fulfilled -> int`.
  - `ProviderError(Exception)` with `provider:str, message:str`; subclasses `AuthError, NotFoundError, CapacityError, ReservedInstanceError, InvalidArgumentError, PreconditionError, UnsupportedOperationError, FleetUnfulfilledError`.
  - `logging_setup.configure(verbose:bool)`, `logging_setup.get_logger(name)`.

- [ ] **Step 1: Write failing test** `candidate/tests/test_errors.py`

```python
from vm.errors import ProviderError, CapacityError, AuthError
from vm.models import State, Instance, CreateResult


def test_provider_error_carries_provider_and_str():
    e = CapacityError("nebius", "no capacity for h100.8x")
    assert e.provider == "nebius"
    assert isinstance(e, ProviderError)
    assert "nebius" in str(e) and "no capacity" in str(e)


def test_create_result_fulfilled_counts_successes():
    inst = Instance(id="i1", name="n", provider="lambda", gpu_type="h100.8x",
                    region="us-west-1", state=State.RUNNING, public_ip=None,
                    private_ip=None, reserved=False, reservation_id=None,
                    price_per_hour=27.68)
    r = CreateResult(requested=2, successes=[inst], errors=[AuthError("lambda", "bad key")])
    assert r.fulfilled == 1
```

- [ ] **Step 2: Run — expect fail** `cd candidate && python -m pytest tests/test_errors.py -q` → ImportError/fail.

- [ ] **Step 3: Implement `candidate/vm/errors.py`**

```python
class ProviderError(Exception):
    """Base for all normalized provider failures. Always names its provider."""

    def __init__(self, provider: str, message: str):
        self.provider = provider
        self.message = message
        super().__init__(f"[{provider}] {message}")


class AuthError(ProviderError): ...
class NotFoundError(ProviderError): ...
class CapacityError(ProviderError): ...
class ReservedInstanceError(ProviderError): ...
class InvalidArgumentError(ProviderError): ...
class PreconditionError(ProviderError): ...
class UnsupportedOperationError(ProviderError): ...


class FleetUnfulfilledError(Exception):
    """Fleet request could not be satisfied; rollback already attempted."""

    def __init__(self, message: str, rolled_back: int, undestroyable: list[str]):
        self.rolled_back = rolled_back
        self.undestroyable = undestroyable
        super().__init__(message)
```

- [ ] **Step 4: Implement `candidate/vm/models.py`**

```python
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
```

- [ ] **Step 5: Implement `candidate/vm/logging_setup.py`**

```python
import logging
import sys

_CONFIGURED = False


def configure(verbose: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
```

- [ ] **Step 6: Run — expect pass** `cd candidate && python -m pytest tests/test_errors.py -q`

- [ ] **Step 7: Commit**

```bash
git add candidate/vm/models.py candidate/vm/errors.py candidate/vm/logging_setup.py candidate/tests/test_errors.py
git commit -m "feat: normalized model, provider-tagged errors, logging"
```

---

## Task 3: GPU catalog + support matrix

**Files:**
- Create: `candidate/vm/catalog.py`
- Test: `candidate/tests/test_catalog.py`

**Interfaces:**
- Produces:
  - `CANONICAL: list[str]` of the 6 GPU types.
  - `to_native(provider:str, gpu:str) -> dict` returning provider-specific fields, e.g. Crusoe `{"type": "h100.8x"}`, Lambda `{"instance_type_name": "gpu_8x_h100"}`, Nebius `{"platform": "gpu-h100-sxm", "preset": "8gpu-160vcpu-1600gb"}`. Raises `InvalidArgumentError` on unknown canonical, `UnsupportedOperationError` if provider lacks it.
  - `supports(provider:str, gpu:str) -> bool`.
  - `providers_for(gpu:str) -> list[str]` (eligible providers for a canonical type).
  - `price(gpu:str) -> float | None`.

- [ ] **Step 1: Write failing test** `candidate/tests/test_catalog.py`

```python
import pytest
from vm import catalog
from vm.errors import UnsupportedOperationError, InvalidArgumentError


def test_to_native_per_provider():
    assert catalog.to_native("crusoe", "h100.8x") == {"type": "h100.8x"}
    assert catalog.to_native("lambda", "h100.8x") == {"instance_type_name": "gpu_8x_h100"}
    assert catalog.to_native("nebius", "h100.8x") == {
        "platform": "gpu-h100-sxm", "preset": "8gpu-160vcpu-1600gb"}


def test_nebius_rejects_a100():
    assert not catalog.supports("nebius", "a100.8x")
    with pytest.raises(UnsupportedOperationError):
        catalog.to_native("nebius", "a100.8x")


def test_providers_for_h200_is_nebius_only():
    assert catalog.providers_for("h200.8x") == ["nebius"]


def test_unknown_gpu_raises():
    with pytest.raises(InvalidArgumentError):
        catalog.to_native("crusoe", "v100.1x")
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `candidate/vm/catalog.py`**

```python
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
```

- [ ] **Step 3b: Append region tests to `candidate/tests/test_catalog.py`**

```python
def test_region_round_trip():
    assert catalog.region_to_native("crusoe", "us-west") == "us-west1"
    assert catalog.region_to_native("lambda", "us-west") == "us-west-1"
    assert catalog.region_to_native("nebius", "us-west") is None
    assert catalog.region_to_canonical("lambda", "us-west-1") == "us-west"
    assert catalog.region_to_canonical("crusoe", "us-west1") == "us-west"


def test_unknown_region_raises():
    with pytest.raises(InvalidArgumentError):
        catalog.region_to_native("crusoe", "mars-1")
```

- [ ] **Step 4: Run — expect pass.**

- [ ] **Step 5: Commit**

```bash
git add candidate/vm/catalog.py candidate/tests/test_catalog.py
git commit -m "feat: GPU catalog + per-provider GPU/region mapping and support matrix"
```

---

## Task 4: Config

**Files:**
- Create: `candidate/vm/config.py`

**Interfaces:**
- Produces:
  - `CRUSOE`, `LAMBDA`, `NEBIUS` config dataclass instances with fields `base_url`/`endpoint`, `api_key`, scope (`project`/`parent`), `default_region`, `ssh_key`.
  - `GENERATED_PATH: str` (absolute path to `mock_servers/generated`).
  - Env overrides: `VM_<PROVIDER>_URL`, `VM_<PROVIDER>_KEY`.

- [ ] **Step 1: Implement `candidate/vm/config.py`**

```python
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATED_PATH = str(REPO_ROOT / "mock_servers" / "generated")


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    api_key: str
    scope: str          # project id / parent id ("" for Lambda)
    default_region: str | None   # CANONICAL region (see catalog); None = no region concept
    ssh_key: str


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


CRUSOE = ProviderConfig(
    base_url=_env("VM_CRUSOE_URL", "http://localhost:8001"),
    api_key=_env("VM_CRUSOE_KEY", "crusoe-test-key-001"),
    scope="proj-001", default_region="us-west", ssh_key="default-key",
)
LAMBDA = ProviderConfig(
    base_url=_env("VM_LAMBDA_URL", "http://localhost:8002"),
    api_key=_env("VM_LAMBDA_KEY", "lambda-test-key-001"),
    scope="", default_region="us-west", ssh_key="default-key",
)
NEBIUS = ProviderConfig(
    base_url=_env("VM_NEBIUS_URL", "localhost:50051"),
    api_key=_env("VM_NEBIUS_KEY", "nebius-test-key-001"),
    scope="project-e1a2b3c4", default_region=None, ssh_key="default-key",
)
```

- [ ] **Step 2: Verify** `cd candidate && python -c "from vm import config; print(config.GENERATED_PATH)"` → path ending `/mock_servers/generated`.

- [ ] **Step 3: Commit**

```bash
git add candidate/vm/config.py
git commit -m "feat: provider config with env overrides and generated-stub path"
```

---

## Task 5: Provider base + capabilities + await helper

**Files:**
- Create: `candidate/vm/providers/__init__.py`, `candidate/vm/providers/base.py`

**Interfaces:**
- Produces:
  - `Capabilities` dataclass: `supports_stop_start:bool, native_batch:bool, queryable_capacity:bool`.
  - `Provider` ABC with abstract `list/get/create/stop/start/destroy/capacity` and concrete `_await(check, timeout=25.0, interval=0.4)` that polls `check()` until it returns non-None, else raises `TimeoutError`.
  - Each provider exposes `name: str` and `capabilities: Capabilities`.

- [ ] **Step 1: Implement `candidate/vm/providers/base.py`**

```python
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
```

`candidate/vm/providers/__init__.py`: empty.

- [ ] **Step 2: Verify** `cd candidate && python -c "from vm.providers.base import Provider, Capabilities; print('ok')"` → `ok`.

- [ ] **Step 3: Commit**

```bash
git add candidate/vm/providers/__init__.py candidate/vm/providers/base.py
git commit -m "feat: Provider ABC, capabilities, poll-until-state helper"
```

---

## Task 6: Lambda provider (synchronous REST)

**Files:**
- Create: `candidate/vm/providers/lambda_.py`
- Test: `candidate/tests/conftest.py`, `candidate/tests/test_providers_integration.py` (Lambda cases)

**Interfaces:**
- Consumes: `catalog`, `config.LAMBDA`, `models`, `errors`, `base.Provider/Capabilities`.
- Produces: `LambdaProvider()` with `name="lambda"`, `capabilities=Capabilities(supports_stop_start=False, native_batch=True, queryable_capacity=False)`.

**Behaviour notes (from mock):** launch is synchronous (instances already `active`); `POST /api/v1/instance-operations/launch` accepts `quantity`; error bodies are `{"detail": {"error": {"code","message","suggestion"}}}`; reserved instances 400 on terminate; no stop/start endpoints.

- [ ] **Step 1: Write conftest with per-provider fixtures** `candidate/tests/conftest.py`

Per-provider session fixtures so provider test files are isolated (each starts only its own
server, and only if its port isn't already serving — safe when servers are pre-started or
when agents run provider suites in parallel on distinct ports).

```python
import os
import socket
import subprocess
import sys
import time
import pytest

MOCK_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mock_servers"))
_VENV_PY = os.path.join(MOCK_DIR, ".venv", "bin", "python")
PY = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable


def _port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _http_ready(url):
    import httpx
    try:
        httpx.get(url, timeout=1.0)
        return True
    except Exception:
        return False


def _ensure(script, port, ready):
    """Start one mock server if its port isn't already serving. Return proc or None."""
    if _port_open("localhost", port):
        return None
    proc = subprocess.Popen([PY, script], cwd=MOCK_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if ready():
            return proc
        time.sleep(0.5)
    proc.terminate()
    pytest.skip(f"{script} did not start")


@pytest.fixture(scope="session")
def lambda_server():
    proc = _ensure("lambda_server.py", 8002, lambda: _http_ready("http://localhost:8002/docs"))
    yield
    if proc:
        proc.terminate()


@pytest.fixture(scope="session")
def crusoe_server():
    proc = _ensure("crusoe_server.py", 8001, lambda: _http_ready("http://localhost:8001/docs"))
    yield
    if proc:
        proc.terminate()


@pytest.fixture(scope="session")
def nebius_server():
    proc = _ensure("nebius_server.py", 50051, lambda: _port_open("localhost", 50051))
    time.sleep(1.0)
    yield
    if proc:
        proc.terminate()


@pytest.fixture(scope="session")
def all_servers(lambda_server, crusoe_server, nebius_server):
    yield
```

- [ ] **Step 2: Write failing test** `candidate/tests/test_lambda.py`

```python
import pytest
from vm.providers.lambda_ import LambdaProvider
from vm.models import State
from vm.errors import UnsupportedOperationError, ReservedInstanceError

pytestmark = pytest.mark.integration


def test_lambda_list_and_create_and_destroy(lambda_server):
    p = LambdaProvider()
    before = p.list()
    assert any(i.provider == "lambda" for i in before)

    res = p.create("h100.1x", count=1, name="plan-test")
    assert res.fulfilled == 1
    inst = res.successes[0]
    assert inst.state == State.RUNNING and inst.gpu_type == "h100.1x"
    assert inst.region == "us-west"  # canonical region reported

    got = p.get(inst.id)
    assert got.id == inst.id
    p.destroy(inst.id)


def test_lambda_stop_unsupported(lambda_server):
    p = LambdaProvider()
    with pytest.raises(UnsupportedOperationError):
        p.stop("anything")


def test_lambda_cannot_terminate_reserved(lambda_server):
    p = LambdaProvider()
    reserved = next(i for i in p.list() if i.reserved)
    with pytest.raises(ReservedInstanceError):
        p.destroy(reserved.id)


def test_lambda_capacity_reports_canonical_regions(lambda_server):
    slots = LambdaProvider().capacity("h100.1x")
    assert slots is not None
    assert all(s.available is None for s in slots)        # boolean presence, no counts
    assert all(s.region in ("us-west", "us-east", "eu-west") for s in slots)
```

- [ ] **Step 3: Run — expect fail** `cd candidate && python -m pytest tests/test_lambda.py -q`

- [ ] **Step 4: Implement `candidate/vm/providers/lambda_.py`**

```python
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
                      if catalog.to_native("lambda", c)["instance_type_name"] == gpu_name), gpu_name)
        return Instance(
            id=d["id"], name=d.get("name") or "", provider=self.name, gpu_type=canon,
            region=(d.get("region") or {}).get("name"),
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
```

- [ ] **Step 5: Run — expect pass** `cd candidate && python -m pytest tests/test_lambda.py -q`

- [ ] **Step 6: Commit**

```bash
git add candidate/vm/providers/lambda_.py candidate/tests/conftest.py candidate/tests/test_lambda.py
git commit -m "feat: Lambda provider (sync REST) + integration harness"
```

---

## Task 7: Crusoe provider (async REST, project-scoped)

**Files:**
- Create: `candidate/vm/providers/crusoe.py`
- Test: add Crusoe cases to `candidate/tests/test_providers_integration.py`

**Interfaces:**
- Produces: `CrusoeProvider()` with `name="crusoe"`, `capabilities=Capabilities(supports_stop_start=True, native_batch=False, queryable_capacity=True)`.

**Behaviour notes:** all URLs prefixed `/v1alpha5/projects/proj-001`; create/stop/start/destroy async → poll instance state; `GET .../instances/{id}` returns the VM dict directly (404 on missing → destroy completion signal); error bodies `{"detail": {"code","message"}}`; `/capacity` gives exact `on_demand_available`+`reserved_available`.

- [ ] **Step 1: Write failing test** `candidate/tests/test_crusoe.py`

```python
import pytest
from vm.providers.crusoe import CrusoeProvider
from vm.models import State
from vm.errors import NotFoundError

pytestmark = pytest.mark.integration


def test_crusoe_full_lifecycle(crusoe_server):
    p = CrusoeProvider()
    res = p.create("h100.1x", count=1, name="crusoe-plan", region="us-west")
    assert res.fulfilled == 1
    inst = res.successes[0]
    assert inst.state == State.RUNNING
    assert inst.region == "us-west"  # canonical
    stopped = p.stop(inst.id)
    assert stopped.state == State.STOPPED
    started = p.start(inst.id)
    assert started.state == State.RUNNING
    p.destroy(inst.id)
    with pytest.raises(NotFoundError):
        p.get(inst.id)


def test_crusoe_capacity_reports_counts_and_canonical_regions(crusoe_server):
    slots = CrusoeProvider().capacity("h100.8x")
    assert slots is not None
    assert all(s.available is not None for s in slots)
    assert all(s.region in ("us-west", "us-east", "eu-west") for s in slots)


def test_crusoe_create_autoselects_region_when_unspecified(crusoe_server):
    p = CrusoeProvider()
    res = p.create("h100.1x", count=1, name="crusoe-auto")  # region=None → auto-select
    assert res.fulfilled == 1
    p.destroy(res.successes[0].id)
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `candidate/vm/providers/crusoe.py`**

```python
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
            region=d.get("location"), state=_STATE.get(d.get("state", ""), State.UNKNOWN),
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
        raise last_err  # exhausted all regions with CapacityError

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
```

- [ ] **Step 4: Run — expect pass** `cd candidate && python -m pytest tests/test_crusoe.py -q`

- [ ] **Step 5: Commit**

```bash
git add candidate/vm/providers/crusoe.py candidate/tests/test_crusoe.py
git commit -m "feat: Crusoe provider (async REST, project-scoped, poll-by-state, region iteration)"
```

---

## Task 8: Nebius provider (gRPC)

**Files:**
- Create: `candidate/vm/providers/nebius.py`
- Test: add Nebius cases to `candidate/tests/test_providers_integration.py`

**Interfaces:**
- Produces: `NebiusProvider()` with `name="nebius"`, `capabilities=Capabilities(supports_stop_start=True, native_batch=False, queryable_capacity=False)`.

**Behaviour notes:** import stubs by inserting `config.GENERATED_PATH` on `sys.path`; metadata `("authorization", "Bearer <key>")`; **no operation-polling RPC** → poll `Get(resource_id)`; state is an int enum; `capacity()` returns `None` (no endpoint); errors are `grpc.RpcError` with `.code()`.

- [ ] **Step 1: Write failing test** `candidate/tests/test_nebius.py`

```python
import pytest
from vm.providers.nebius import NebiusProvider
from vm.models import State
from vm.errors import AuthError

pytestmark = pytest.mark.integration


def test_nebius_list_and_create(nebius_server):
    p = NebiusProvider()
    assert any(i.provider == "nebius" for i in p.list())
    res = p.create("h100.1x", count=1, name="neb-plan")
    assert res.fulfilled == 1
    inst = res.successes[0]
    assert inst.state == State.RUNNING
    assert inst.region is None  # Nebius has no region concept
    p.destroy(inst.id)


def test_nebius_capacity_unknown(nebius_server):
    assert NebiusProvider().capacity("h100.8x") is None


def test_nebius_auth_error(nebius_server):
    p = NebiusProvider(api_key="bad-key")
    with pytest.raises(AuthError):
        p.list()
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `candidate/vm/providers/nebius.py`**

```python
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
```

- [ ] **Step 4: Run — expect pass** `cd candidate && python -m pytest tests/test_nebius.py -q`

- [ ] **Step 5: Commit**

```bash
git add candidate/vm/providers/nebius.py candidate/tests/test_nebius.py
git commit -m "feat: Nebius provider (gRPC, poll-by-state, capacity unknown)"
```

---

## Task 9: Registry + output + Layer 1 CLI

**Files:**
- Create: `candidate/vm/providers/registry.py`, `candidate/vm/output.py`, `candidate/vm/cli.py`
- Test: `candidate/tests/test_cli_smoke.py` (unit, no servers, for arg parsing + error formatting)

**Interfaces:**
- Produces:
  - `registry.get(name) -> Provider`; `registry.all() -> list[Provider]`; `registry.NAMES`.
  - `output.render_instances(list[Instance], as_json:bool) -> str`; `output.render_capacity(...)`.
  - `cli.main(argv=None) -> int`.

- [ ] **Step 1: Implement `candidate/vm/providers/registry.py`**

```python
from vm.providers.crusoe import CrusoeProvider
from vm.providers.lambda_ import LambdaProvider
from vm.providers.nebius import NebiusProvider

_FACTORIES = {"crusoe": CrusoeProvider, "lambda": LambdaProvider, "nebius": NebiusProvider}
NAMES = tuple(_FACTORIES.keys())


def get(name: str):
    if name not in _FACTORIES:
        raise ValueError(f"unknown provider '{name}' (known: {', '.join(NAMES)})")
    return _FACTORIES[name]()


def all():
    return [f() for f in _FACTORIES.values()]
```

- [ ] **Step 2: Implement `candidate/vm/output.py`**

```python
import json
from dataclasses import asdict
from vm.models import Instance


def _table(rows: list[list[str]], headers: list[str]) -> str:
    cols = list(zip(*([headers] + rows))) if rows else [[h] for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths))
    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def render_instances(instances: list[Instance], as_json: bool = False) -> str:
    if as_json:
        return json.dumps([{**asdict(i), "state": i.state.value} for i in instances], indent=2)
    if not instances:
        return "(no instances)"
    headers = ["PROVIDER", "ID", "NAME", "GPU", "REGION", "STATE", "PUBLIC_IP", "RSVD"]
    rows = [[i.provider, i.id[:12], i.name, i.gpu_type, i.region or "-",
             i.state.value, i.public_ip or "-", "yes" if i.reserved else "no"] for i in instances]
    return _table(rows, headers)
```

- [ ] **Step 3: Implement `candidate/vm/cli.py`**

```python
from __future__ import annotations
import argparse
import sys

from vm import output
from vm.logging_setup import configure, get_logger
from vm.errors import ProviderError
from vm.providers import registry

log = get_logger("vm")


def _cmd_list(args) -> int:
    providers = [registry.get(args.provider)] if args.provider else registry.all()
    instances = []
    for p in providers:
        try:
            instances += p.list()
        except ProviderError as e:
            log.error("[%s] list failed: %s", e.provider, e.message)
    print(output.render_instances(instances, args.json))
    return 0


def _cmd_create(args) -> int:
    p = registry.get(args.provider)
    res = p.create(args.gpu, count=args.count, name=args.name, region=args.region)
    for e in res.errors:
        log.error("[%s] create failed: %s", e.provider, e.message)
    print(output.render_instances(res.successes, args.json))
    return 0 if res.fulfilled == res.requested else 1


def _cmd_get(args) -> int:
    print(output.render_instances([registry.get(args.provider).get(args.id)], args.json))
    return 0


def _cmd_stop(args) -> int:
    inst = registry.get(args.provider).stop(args.id)
    print(f"{inst.id} -> {inst.state.value}")
    return 0


def _cmd_start(args) -> int:
    inst = registry.get(args.provider).start(args.id)
    print(f"{inst.id} -> {inst.state.value}")
    return 0


def _cmd_destroy(args) -> int:
    registry.get(args.provider).destroy(args.id)
    print(f"{args.id} destroyed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vm", description="Unified GPU VM manager")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--provider", choices=registry.NAMES)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_create = sub.add_parser("create")
    p_create.add_argument("--provider", required=True, choices=registry.NAMES)
    p_create.add_argument("--gpu", required=True)
    p_create.add_argument("--count", type=int, default=1)
    p_create.add_argument("--name")
    p_create.add_argument("--region")
    p_create.add_argument("--json", action="store_true")
    p_create.set_defaults(func=_cmd_create)

    for cmd, func in [("get", _cmd_get), ("stop", _cmd_stop), ("start", _cmd_start), ("destroy", _cmd_destroy)]:
        pc = sub.add_parser(cmd)
        pc.add_argument("id")
        pc.add_argument("--provider", required=True, choices=registry.NAMES)
        pc.add_argument("--json", action="store_true")
        pc.set_defaults(func=func)

    # fleet subcommands are attached in Task 13
    attach_fleet = getattr(sys.modules.get("vm.cli"), "_attach_fleet", None)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    from vm import fleet_cli  # noqa: F401  (registers fleet subparser if present)
    args = parser.parse_args(argv)
    configure(getattr(args, "verbose", False))
    try:
        return args.func(args)
    except ProviderError as e:
        print(f"Error [{e.provider}]: {e.message}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

> NOTE for executor: the `fleet` wiring in `main()`/`build_parser()` above is finalized in Task 13. For Task 9, remove the `attach_fleet`/`from vm import fleet_cli` lines so Layer 1 runs standalone; re-add them in Task 13. Keep Task 9's parser to the six Layer 1 commands.

- [ ] **Step 4: Write smoke test** `candidate/tests/test_cli_smoke.py`

```python
from vm.cli import build_parser


def test_parser_has_layer1_commands():
    parser = build_parser()
    args = parser.parse_args(["list", "--provider", "lambda", "--json"])
    assert args.command == "list" and args.provider == "lambda" and args.json is True


def test_create_requires_provider_and_gpu():
    parser = build_parser()
    ns = parser.parse_args(["create", "--provider", "crusoe", "--gpu", "h100.8x", "--count", "2"])
    assert ns.count == 2 and ns.gpu == "h100.8x"
```

- [ ] **Step 5: Run smoke test — expect pass** `cd candidate && python -m pytest tests/test_cli_smoke.py -q`

- [ ] **Step 6: Manual end-to-end (servers running in another shell via `bash mock_servers/start_all.sh`)**

Run: `cd candidate && python -m vm list`
Expected: a table with seeded instances from all three providers.
Run: `python -m vm create --provider lambda --gpu h100.1x --name demo`
Expected: one RUNNING row.

- [ ] **Step 7: Commit**

```bash
git add candidate/vm/providers/registry.py candidate/vm/output.py candidate/vm/cli.py candidate/tests/test_cli_smoke.py
git commit -m "feat: Layer 1 CLI (list/create/get/stop/start/destroy) with provider-tagged errors"
```

---

## Task 10: Fleet store (JSON persistence)

**Files:**
- Create: `candidate/vm/fleet/__init__.py`, `candidate/vm/fleet/store.py`
- Test: `candidate/tests/test_store.py`

**Interfaces:**
- Produces:
  - `FleetRecord` dataclass: `name:str, gpu:str, created_at:str, status:str, vms:list[dict]` (each vm `{"provider","id","region"}`).
  - `Store(path=None)` with `save(record)`, `get(name)->FleetRecord|None`, `list()->list[FleetRecord]`, `delete(name)`. Atomic write via temp file + `os.replace`. Default path `~/.vmfleet/fleets.json`; test injects a tmp path.

- [ ] **Step 1: Write failing test** `candidate/tests/test_store.py`

```python
from vm.fleet.store import Store, FleetRecord


def test_store_round_trip(tmp_path):
    s = Store(path=tmp_path / "fleets.json")
    rec = FleetRecord(name="f1", gpu="h100.8x", created_at="2026-07-01T00:00:00Z",
                      status="active", vms=[{"provider": "crusoe", "id": "vm1", "region": "us-west1"}])
    s.save(rec)
    got = s.get("f1")
    assert got.gpu == "h100.8x" and got.vms[0]["id"] == "vm1"
    assert [r.name for r in s.list()] == ["f1"]
    s.delete("f1")
    assert s.get("f1") is None


def test_store_persists_across_instances(tmp_path):
    p = tmp_path / "fleets.json"
    Store(path=p).save(FleetRecord("f2", "h100.1x", "t", "active", []))
    assert Store(path=p).get("f2") is not None
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `candidate/vm/fleet/store.py`**

```python
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
```

`candidate/vm/fleet/__init__.py`: empty.

- [ ] **Step 4: Run — expect pass.**

- [ ] **Step 5: Commit**

```bash
git add candidate/vm/fleet/__init__.py candidate/vm/fleet/store.py candidate/tests/test_store.py
git commit -m "feat: fleet JSON store with atomic writes"
```

---

## Task 11: Fleet scheduler (capacity query + allocation)

**Files:**
- Create: `candidate/vm/fleet/scheduler.py`
- Test: `candidate/tests/test_scheduler.py`

**Interfaces:**
- Consumes: `catalog`, `models.CapacitySlot`.
- Produces:
  - `known_capacity(provider) -> int | None`: sums a provider's `capacity(gpu)` slots; `None` if unqueryable or slots have `None` counts.
  - `allocate(gpu:str, count:int, providers:list[Provider]) -> list[tuple[str,int]]`: eligible providers only (via `catalog.supports`), ordered by known capacity desc (unknown → overflow, placed last), assigning up to known capacity then remainder spread to unknown/last providers. Pure function given the providers' `capacity()` outputs — test with fakes.

- [ ] **Step 1: Write failing test** `candidate/tests/test_scheduler.py`

```python
from vm.fleet import scheduler
from vm.models import CapacitySlot


class FakeProvider:
    def __init__(self, name, slots):
        self.name = name
        self._slots = slots  # None = capacity unknown

    def capacity(self, gpu):
        return self._slots


def test_allocate_round_robin_spreads_evenly_across_unknown():
    a, b, c = (FakeProvider("crusoe", None), FakeProvider("lambda", None), FakeProvider("nebius", None))
    plan = dict(scheduler.allocate("h100.8x", 6, [a, b, c]))
    assert plan == {"crusoe": 2, "lambda": 2, "nebius": 2}  # even spread


def test_allocate_small_count_spans_multiple_providers():
    a, b, c = (FakeProvider("crusoe", None), FakeProvider("lambda", None), FakeProvider("nebius", None))
    plan = dict(scheduler.allocate("h100.8x", 2, [a, b, c]))
    assert sum(plan.values()) == 2 and len(plan) == 2  # spread across 2, not packed onto 1


def test_allocate_caps_known_capacity_provider_then_overflows_to_unknown():
    crusoe = FakeProvider("crusoe", [CapacitySlot("crusoe", "h100.8x", "us-west", 3)])
    nebius = FakeProvider("nebius", None)  # unknown → uncapped overflow
    plan = dict(scheduler.allocate("h100.8x", 8, [crusoe, nebius]))
    assert plan == {"crusoe": 3, "nebius": 5}  # crusoe capped at its known 3


def test_allocate_skips_unsupported_providers():
    lam = FakeProvider("lambda", [CapacitySlot("lambda", "h200.8x", "us-west", None)])
    neb = FakeProvider("nebius", None)
    plan = scheduler.allocate("h200.8x", 2, [lam, neb])  # lambda can't do h200
    assert plan == [("nebius", 2)]


def test_allocate_all_finite_below_request_returns_partial_plan():
    a = FakeProvider("crusoe", [CapacitySlot("crusoe", "h100.8x", "us-west", 2)])
    b = FakeProvider("lambda", [CapacitySlot("lambda", "h100.8x", "us-west", 1)])
    # both finite, total 3 < 5 → plan sums to 3; manager surfaces the shortfall + rolls back
    plan = dict(scheduler.allocate("h100.8x", 5, [a, b]))
    assert sum(plan.values()) == 3
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `candidate/vm/fleet/scheduler.py`**

```python
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
```

- [ ] **Step 4: Run — expect pass.**

- [ ] **Step 5: Commit**

```bash
git add candidate/vm/fleet/scheduler.py candidate/tests/test_scheduler.py
git commit -m "feat: fleet scheduler (capacity-aware allocation with overflow)"
```

---

## Task 12: Fleet manager (concurrency + failover + rollback)

**Files:**
- Create: `candidate/vm/fleet/manager.py`
- Test: `candidate/tests/test_fleet_integration.py`

**Interfaces:**
- Consumes: `registry`, `scheduler`, `store`, `errors`, `models`.
- Produces `FleetManager(store=None, providers=None)`:
  - `create(gpu, count, name) -> FleetRecord` — allocate → execute concurrently with failover → rollback on shortfall (`FleetUnfulfilledError`) → persist on success.
  - `list() -> list[FleetRecord]`.
  - `status(name) -> dict` — record + live per-VM state via concurrent `get`, flags `MISSING`.
  - `destroy(name) -> dict` — concurrent teardown; returns `{"destroyed":n, "left_reserved":[ids]}`; removes from store.

- [ ] **Step 1: Write failing test** `candidate/tests/test_fleet_integration.py`

```python
import pytest
from vm.fleet.manager import FleetManager
from vm.fleet.store import Store
from vm.errors import FleetUnfulfilledError

pytestmark = pytest.mark.integration


def test_fleet_create_spans_providers_and_status(all_servers, tmp_path):
    fm = FleetManager(store=Store(path=tmp_path / "f.json"))
    rec = fm.create("h100.8x", count=3, name="spanfleet")
    assert len(rec.vms) == 3
    providers_used = {v["provider"] for v in rec.vms}
    # round-robin spreads a 3-VM request across the eligible providers
    assert len(providers_used) >= 2
    st = fm.status("spanfleet")
    assert st["name"] == "spanfleet" and len(st["vms"]) == 3
    assert all(v["state"] in ("RUNNING", "CREATING") for v in st["vms"])
    fm.destroy("spanfleet")
    assert fm.store.get("spanfleet") is None


def test_fleet_rollback_when_unfulfillable(all_servers, tmp_path):
    fm = FleetManager(store=Store(path=tmp_path / "f.json"))
    # total h100.8x capacity across providers is ~14; 20 forces a shortfall → rollback
    with pytest.raises(FleetUnfulfilledError):
        fm.create("h100.8x", count=20, name="toobig")
    assert fm.store.get("toobig") is None  # nothing persisted, everything rolled back
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement `candidate/vm/fleet/manager.py`**

```python
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
```

- [ ] **Step 4: Run — expect pass** `cd candidate && python -m pytest tests/test_fleet_integration.py -q -m integration`

- [ ] **Step 5: Commit**

```bash
git add candidate/vm/fleet/manager.py candidate/tests/test_fleet_integration.py
git commit -m "feat: fleet manager with concurrency, failover, rollback, reconcile"
```

---

## Task 13: Fleet CLI wiring

**Files:**
- Modify: `candidate/vm/cli.py`
- Create: `candidate/vm/fleet_cli.py`
- Test: extend `candidate/tests/test_cli_smoke.py`

**Interfaces:**
- Produces: `fleet_cli.attach(subparsers)` adding `fleet create|list|status|destroy`; handlers use `FleetManager`.
- Modifies `cli.build_parser()` to call `fleet_cli.attach(sub)`; removes the Task-9 placeholder note lines.

- [ ] **Step 1: Implement `candidate/vm/fleet_cli.py`**

```python
from __future__ import annotations
from vm.fleet.manager import FleetManager
from vm.errors import FleetUnfulfilledError


def _fleet_create(args) -> int:
    fm = FleetManager()
    try:
        rec = fm.create(args.gpu, count=args.count, name=args.name)
    except FleetUnfulfilledError as e:
        print(f"Fleet failed: {e} (rolled back {e.rolled_back}; "
              f"{len(e.undestroyable)} left reserved)")
        return 1
    print(f"Fleet '{rec.name}' created: {len(rec.vms)} VMs")
    for v in rec.vms:
        print(f"  {v['provider']:8s} {v['id'][:12]} {v['region'] or '-'}")
    return 0


def _fleet_list(args) -> int:
    fleets = FleetManager().list()
    if not fleets:
        print("(no fleets)")
        return 0
    for f in fleets:
        print(f"{f.name:16s} gpu={f.gpu:8s} vms={len(f.vms):3d} status={f.status}")
    return 0


def _fleet_status(args) -> int:
    st = FleetManager().status(args.name)
    print(f"Fleet '{st['name']}' gpu={st['gpu']} status={st['status']}")
    for v in st["vms"]:
        print(f"  {v['provider']:8s} {v['id'][:12]} {v.get('region') or '-':10s} {v['state']}")
    return 0


def _fleet_destroy(args) -> int:
    res = FleetManager().destroy(args.name)
    print(f"Destroyed {res['destroyed']} VMs; {len(res['left_reserved'])} left reserved")
    return 0


def attach(sub) -> None:
    fleet = sub.add_parser("fleet")
    fsub = fleet.add_subparsers(dest="fleet_command", required=True)

    c = fsub.add_parser("create")
    c.add_argument("--gpu", required=True)
    c.add_argument("--count", type=int, required=True)
    c.add_argument("--name", required=True)
    c.set_defaults(func=_fleet_create)

    fsub.add_parser("list").set_defaults(func=_fleet_list)

    s = fsub.add_parser("status")
    s.add_argument("name")
    s.set_defaults(func=_fleet_status)

    d = fsub.add_parser("destroy")
    d.add_argument("name")
    d.set_defaults(func=_fleet_destroy)
```

- [ ] **Step 2: Modify `cli.build_parser()`** — replace the Task-9 placeholder block. After the Layer 1 subparsers and before `return parser`:

```python
    from vm import fleet_cli
    fleet_cli.attach(sub)
    return parser
```

And in `main()`, remove the `from vm import fleet_cli  # noqa` import line (now imported inside `build_parser`). The final `main()`:

```python
def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure(getattr(args, "verbose", False))
    try:
        return args.func(args)
    except ProviderError as e:
        print(f"Error [{e.provider}]: {e.message}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
```

- [ ] **Step 3: Extend smoke test** (append to `test_cli_smoke.py`)

```python
def test_fleet_create_parses():
    from vm.cli import build_parser
    ns = build_parser().parse_args(["fleet", "create", "--gpu", "h100.8x", "--count", "4", "--name", "f"])
    assert ns.fleet_command == "create" and ns.count == 4
```

- [ ] **Step 4: Run smoke test — expect pass.**

- [ ] **Step 5: Manual e2e (servers running):**

Run: `cd candidate && python -m vm fleet create --gpu h100.8x --count 3 --name demo`
Expected: "Fleet 'demo' created: 3 VMs" spread across providers.
Run: `python -m vm fleet status demo` then `python -m vm fleet destroy demo`.

- [ ] **Step 6: Commit**

```bash
git add candidate/vm/cli.py candidate/vm/fleet_cli.py candidate/tests/test_cli_smoke.py
git commit -m "feat: fleet CLI subcommands (create/list/status/destroy)"
```

---

## Task 14: Docs + full test pass

**Files:**
- Create: `candidate/README.md`

**Interfaces:** none (documentation + verification).

- [ ] **Step 1: Write `candidate/README.md`**

Contents: how to install (`pip install -e candidate` or `pip install httpx grpcio protobuf`), how to start mock servers (`bash mock_servers/start_all.sh`), the Layer 1 + Layer 2 command examples, a short "design" paragraph pointing to the spec, and how to run tests (`cd candidate && python -m pytest -q` for unit; `-m integration` for integration).

- [ ] **Step 2: Run unit tests (no servers)**

Run: `cd candidate && python -m pytest -q -m "not integration"`
Expected: all of `test_errors, test_catalog, test_store, test_scheduler, test_cli_smoke` pass.

- [ ] **Step 3: Run integration tests (fixture boots servers)**

Run: `cd candidate && python -m pytest -q -m integration`
Expected: provider + fleet integration tests pass (allow ~30-60s for async waits).

- [ ] **Step 4: Commit**

```bash
git add candidate/README.md
git commit -m "docs: candidate README + verified unit/integration test pass"
```

---

## Self-Review

**Spec coverage:**
- §2 adapter + capabilities → Tasks 5–8 (Capabilities on every provider). ✓
- §3 normalized model → Task 2. ✓
- §4 error hierarchy + provider tag → Task 2 (+ translation in Tasks 6–8, surfaced in Tasks 9/12/13). ✓
- §5 async poll-by-state → `_await` in Task 5; used by Crusoe/Nebius (Tasks 7/8); Lambda synchronous (Task 6). ✓
- §6 catalog + support matrix + region normalization → Task 3 (GPU + region maps). ✓
- §7 round-robin scheduler + concurrent round-1 & failover rounds + rollback + store +
  status reconcile → Tasks 10–13; provider-internal region auto-select/iterate → Tasks 6–7. ✓
- §8 config → Task 4. ✓
- §9 CLI surface → Tasks 9 & 13. ✓
- §10 testing (unit + integration) → tests in each task; Task 14 full pass. ✓
- Logging requirement (provider-tagged) → `logging_setup` (Task 2), used across Tasks 9/11/12. ✓

**Placeholder scan:** Task 9 intentionally notes the fleet-wiring seam finalized in Task 13; that seam is fully specified in Task 13 (no vague "TODO"). No other placeholders.

**Type consistency:** `CreateResult(requested, successes, errors)`, `Instance(...)` fields, `Capabilities(supports_stop_start, native_batch, queryable_capacity)`, `FleetRecord(name, gpu, created_at, status, vms)`, `scheduler.allocate(gpu, count, providers) -> [(name, n)]`, `_await(check)` — all used consistently across tasks.

## Execution note

Integration tests boot the real mock servers; ensure `mock_servers/.venv` deps are installed (or run `bash mock_servers/start_all.sh` once) and that the CLI env has `httpx`, `grpcio`, `protobuf`, `pytest`.
