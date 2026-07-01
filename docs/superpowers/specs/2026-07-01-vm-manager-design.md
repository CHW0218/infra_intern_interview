# Unified GPU Cloud VM Manager — Design Spec

**Date:** 2026-07-01
**Status:** Approved (brainstorming) → ready for implementation plan
**Scope:** Implement the tool described in `README.md` (Layer 1 unified CLI + Layer 2 fleet manager), built incrementally.

---

## 1. Goal

A single Python CLI (`vm`) that manages GPU VMs across three providers with different
protocols and semantics, hiding those differences behind one clean abstraction:

- **Crusoe** — REST, project-scoped (`proj-001`), async operations.
- **Lambda** — REST, flat, synchronous, reserved instances can't be terminated.
- **Nebius** — gRPC, parent-scoped (`project-e1a2b3c4`), async operations, reservation policy.

Language: **Python** (mock servers are Python; Nebius gRPC stubs pre-compiled in
`mock_servers/generated/`; docs give Python examples). No proto compilation needed.

Solution lives in `candidate/` (gitignored except `.gitkeep`).

---

## 2. Architecture — Adapter pattern with honest capability modeling ("A′")

A single abstract `Provider` interface; three concrete adapters translate unified calls
into each provider's protocol and translate responses back into a shared normalized model.
The CLI and fleet manager only ever touch the interface + normalized types. Adding a
provider = one new file.

**Why not the alternatives:** a big if/else in the CLI leaks provider differences into the
CLI layer (fails "clean abstraction"); a plugin registry over-engineers for 3 providers
(README warns against over-engineering).

**The leaky-abstraction risk is real, so the interface models differences explicitly
rather than pretending uniformity:**

- **Capability flags** per provider: `supports_stop_start`, `native_batch`,
  `queryable_capacity`, `supported_gpu_types`. CLI/scheduler check capability and degrade
  gracefully instead of assuming everyone can do everything.
- **`UnsupportedOperationError` is a first-class outcome** — e.g. Lambda has no stop/start,
  so `LambdaProvider.stop()` raises it with a clear message (no faking).
- **`capacity()` is best-effort, may return "unknown"** (see §7). The scheduler treats it
  as a hint; correctness comes from attempt → catch → failover → rollback.
- **`create()` returns partial results + per-VM errors**, not a bare list.
- **Rollback tolerates un-destroyable VMs** (Lambda reserved instances).

### Provider interface

```
Provider (abstract)
  name: str
  capabilities: Capabilities
  list()                              -> list[Instance]
  get(id)                             -> Instance
  create(gpu, count, name?, region?, wait=True) -> CreateResult   # successes + errors
  stop(id)                            -> Instance      # UnsupportedOperationError on Lambda
  start(id)                           -> Instance      # UnsupportedOperationError on Lambda
  destroy(id)                         -> None          # ReservedInstanceError possible (Lambda)
  capacity(gpu)                       -> list[CapacitySlot] | None   # None = unknown
```

---

## 3. Normalized model (`models.py`)

```
Instance: id, name, provider, gpu_type (canonical), region, state (State enum),
          public_ip, private_ip, reserved (bool), reservation_id, price_per_hour
State (enum): CREATING, RUNNING, STOPPING, STOPPED, STARTING, DELETING,
              TERMINATED, ERROR, UNKNOWN
CapacitySlot: provider, gpu_type, region, available (int | None)
CreateResult: successes: list[Instance], errors: list[ProviderError]
```

Each provider maps its own states into the one `State` enum
(Crusoe `STATE_RUNNING`, Lambda `active`, Nebius `RUNNING`=2, etc.).

---

## 4. Error handling (`errors.py`)

Every provider translates its native failure into one normalized exception:

| Unified exception | Crusoe | Lambda | Nebius |
|---|---|---|---|
| `AuthError` | 401 `UNAUTHENTICATED` | 401 `invalid-api-key` | `UNAUTHENTICATED`(16) |
| `NotFoundError` | 404 `NOT_FOUND` | `object-does-not-exist` | `NOT_FOUND`(5) |
| `CapacityError` | `RESOURCE_EXHAUSTED` | `insufficient-capacity` | `RESOURCE_EXHAUSTED`(8) |
| `ReservedInstanceError` | — | `terminate/reserved-instance` | — |
| `InvalidArgumentError` | `INVALID_ARGUMENT` | `invalid-parameters` | `INVALID_ARGUMENT`(3) |
| `PreconditionError` | `FAILED_PRECONDITION` | — | `FAILED_PRECONDITION`(9) |
| `UnsupportedOperationError` | raised locally (no network) for unsupported op/GPU | | |
| `ProviderError` | catch-all base for anything unmapped | | |

CLI catches these at the top level → clean one-line message + non-zero exit code.
No raw tracebacks.

---

## 5. Async vs synchronous handling

Completion is judged by **observed resource state**, not the operation object — because
Nebius has **no operation-polling RPC** (the proto's `InstanceService` only exposes
Get/List/Create/Delete/Start/Stop; `Operation.done` is a dead snapshot). The only portable
signal is `Get(id).status.state`.

Each async provider implements one internal helper:

```
_wait_for_state(id, target_states, timeout≈20s, interval≈0.4s)
    poll get(id) until state ∈ target_states   (or NotFound, for destroy)
```

- Crusoe/Nebius `create` → submit, then `_wait_for_state(id, {RUNNING})`.
- Lambda `_wait_for_state` returns immediately (already `active`). **Synchronous = async
  that finishes instantly** — no special-casing for callers.
- `destroy` → poll until `NotFoundError`; `stop` → `{STOPPED}`; `start` → `{RUNNING}`.

Verified in mock code: **capacity/auth/precondition errors are raised synchronously at
submit** (before the async delay), so Layer-2 failover is fast. Waiting is per-instance, so
Layer 2 submits all creates first then waits in parallel (fleet of 8 ≈ 3s, not 8×3s).

---

## 6. GPU catalog + support matrix (`catalog.py`)

Canonical vocabulary the user types: `a100.1x, a100.8x, h100.1x, h100.8x, h200.1x, h200.8x`.

| Canonical | Crusoe | Lambda | Nebius (platform / preset) |
|---|---|---|---|
| `a100.1x` | `a100.1x` | `gpu_1x_a100` | ❌ |
| `a100.8x` | `a100.8x` | `gpu_8x_a100` | ❌ |
| `h100.1x` | `h100.1x` | `gpu_1x_h100` | `gpu-h100-sxm` / `1gpu-16vcpu-200gb` |
| `h100.8x` | `h100.8x` | `gpu_8x_h100` | `gpu-h100-sxm` / `8gpu-160vcpu-1600gb` |
| `h200.1x` | ❌ | ❌ | `gpu-h200-sxm` / `1gpu-20vcpu-256gb` |
| `h200.8x` | ❌ | ❌ | `gpu-h200-sxm` / `8gpu-160vcpu-2048gb` |

Unsupported combos fail before any network call with a helpful message. The catalog also
tells the scheduler which providers are eligible for a fleet request.

---

## 7. Layer 2 — Fleet manager

### Commands
`vm fleet create --gpu <type> --count <n> [--name <fleet_name>]`,
`vm fleet list`, `vm fleet status <name>`, `vm fleet destroy <name>`.

### Scheduler strategy: S1 — capacity-aware greedy + failover
- Query capacity best-effort (Crusoe exact via `/capacity`; Lambda region-level booleans
  via `/instance-types`; Nebius **unknown** — no endpoint).
- Order eligible providers by known availability; unknowns act as overflow buckets.
- Assign counts up to what's known; execute concurrently; **cascade any shortfall /
  `CapacityError` to the next provider** until count met or providers exhausted.
- Satisfies README challenge #1 (query capacity then allocate) and #2 (partial failure →
  get rest from B/C), while treating numbers as hints and relying on attempt+failover for
  correctness.

### Execution (concurrency + rollback)
```
plan = scheduler.allocate(gpu, count)          # [(provider, n), ...] ordered
provisioned = []
with ThreadPoolExecutor() as pool:
    for provider, n in plan:                    # cascade remainder
        res = create_n_concurrently(provider, n)   # submit all, wait in parallel
        provisioned += res.successes
    if len(provisioned) < count:                # can't fulfill → ROLLBACK
        destroy_all_concurrently(provisioned)   # tolerate reserved/un-destroyable
        raise FleetUnfulfilledError(...)
store.save(fleet_name, gpu, provisioned)        # persist only on full success
```

- **Concurrency = `ThreadPoolExecutor`** (not asyncio): Nebius gRPC stubs are synchronous
  and httpx works synchronously — threads unify both protocols with one mechanism.
- **Rollback** destroys concurrently, swallows `ReservedInstanceError`/`NotFoundError`,
  reports what couldn't be cleaned up.
- **`fleet status`** reads store then reconciles: fan-out `get(id)`, show live state, flag
  drift (`MISSING` if deleted out-of-band).
- **`fleet destroy`** = concurrent teardown + remove from store; reserved VMs reported as
  "left running (reserved)".

### Store (`store.py`) — `~/.vmfleet/fleets.json`, atomic write (temp-file rename)
```json
{ "fleets": { "my-fleet": {
    "gpu": "h100.8x", "created_at": "...", "status": "active",
    "vms": [ {"provider":"crusoe","id":"...","region":"us-west1"},
             {"provider":"nebius","id":"...","region":null} ] } } }
```

---

## 8. Config (`config.py`)

Endpoints, API keys, scope IDs, and defaults for fields the unified CLI doesn't model but
the mock servers require (ssh key `default-key`; default region per provider). Overridable
via env vars.

| Provider | Endpoint | Key | Scope |
|---|---|---|---|
| Crusoe | `http://localhost:8001` | `crusoe-test-key-001` | project `proj-001` |
| Lambda | `http://localhost:8002` | `lambda-test-key-001` | — |
| Nebius | `localhost:50051` | `nebius-test-key-001` | parent `project-e1a2b3c4` |

---

## 9. CLI surface (`cli.py`)

```
vm list [--provider <name>] [--json]
vm create --provider <name> --gpu <type> --count <n> [--name <name>] [--region <region>] [--json]
vm get <id> --provider <name> [--json]
vm stop <id> --provider <name>
vm start <id> --provider <name>
vm destroy <id> --provider <name>
vm fleet create --gpu <type> --count <n> [--name <name>]
vm fleet list
vm fleet status <name>
vm fleet destroy <name>
```
Output: table by default (via a small renderer), `--json` for machine-readable.

---

## 10. Testing

- **Unit (no network):** catalog mapping + support matrix, error translation, scheduler
  allocation math, store round-trip + atomicity.
- **Integration (live mock servers):** pytest fixture boots `start_all.sh`, waits ready,
  then per-provider list/create→RUNNING/stop/start/destroy, reserved-can't-terminate path,
  fleet create spanning providers, and fleet rollback.

---

## 11. Build order (incremental)

1. Scaffolding: `models`, `errors`, `config`, `catalog`, `providers/base`, `output`, `cli` skeleton.
2. **Lambda** provider (synchronous, simplest) — `list` + `create` green end-to-end first.
3. **Crusoe** provider (async polling + project scoping).
4. **Nebius** provider (gRPC, wire in generated stubs).
5. Full Layer 1 lifecycle (get/stop/start/destroy) + error mapping across all three.
6. Layer 2: `store` → `scheduler` → `manager` → `fleet` CLI, with concurrency + rollback.
7. Tests throughout; integration pass at the end.

---

## 12. Out of scope (YAGNI)

Crusoe reboot/reset/restart, reservation-aware cost optimization, pagination, real auth/secrets
management, multi-project. Can be added later behind the same interface.
