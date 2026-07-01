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

**Provider identity in every error (required).** `ProviderError` is the base of the whole
hierarchy and carries a `provider: str` field; each adapter sets it when translating a
native failure. So an error always knows where it came from, and both the CLI and the fleet
manager surface it:

- CLI: `Error [crusoe]: no capacity for h100.8x in eu-west1`.
- A small logging setup (`logging.py`, stdlib `logging`) emits `WARNING`/`ERROR` lines
  tagged with the provider — e.g. during fleet scheduling/failover/rollback,
  `logger.error("[nebius] create failed: RESOURCE_EXHAUSTED — failing over")`.
- `CreateResult.errors` and the fleet summary list failures grouped by provider, so a
  partial fleet clearly shows which provider fell short.

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
submit** (before the async delay), so Layer-2 failover is fast. Because waiting is
per-instance, the fleet manager fires **every provider's creates concurrently in a single
round** (§7) — so a fleet of 8 spread across 3 providers costs ≈ one create (~3s), not the
sum. A failover round (only for the shortfall) adds ~3s each; the common no-failover case is
one round.

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

### Region normalization (capacity is partitioned by region × type)

The mocks key capacity by `(region, type)`, and each provider names regions differently
(**Crusoe** `us-west1`, **Lambda** `us-west-1` (extra dash), **Nebius** has *no* region —
capacity is keyed by `platform/preset`). So the catalog also normalizes a **canonical region
vocabulary**: `us-west, us-east, eu-west`.

| Canonical | Crusoe | Lambda | Nebius |
|---|---|---|---|
| `us-west` | `us-west1` | `us-west-1` | — |
| `us-east` | `us-east1` | `us-east-1` | — |
| `eu-west` | `eu-west1` | `eu-west-1` | — |

- `--region` accepts canonical; `provider.create` translates canonical → native. Nebius
  ignores region entirely.
- `capacity()` reports **canonical** regions, so the scheduler never sees a provider dialect.
- **Region is a provider-internal concern.** When `create(region=None)` (the fleet path), the
  provider **auto-selects** a region with capacity and, on `CapacityError`, **retries its
  other regions** ordered by known capacity. This lets a provider gather its *full
  cross-region* capacity (e.g. Crusoe's `rsv-002` = 4 reserved h100.8x nodes live only in
  `us-west1`) without the scheduler having to model regions. The scheduler stays
  `[(provider, n)]`.

---

## 7. Layer 2 — Fleet manager

### Commands
`vm fleet create --gpu <type> --count <n> [--name <fleet_name>]`,
`vm fleet list`, `vm fleet status <name>`, `vm fleet destroy <name>`.

### Scheduler strategy: round-robin even-spread, capped by known capacity
- Query capacity best-effort per provider (Crusoe exact counts via `/capacity`; Lambda
  region-level presence via `/instance-types` — **boolean, no counts**; Nebius **unknown**
  — no endpoint). `known_capacity(provider) = sum of counts` where countable, else `None`.
- **Round-robin** assign one VM at a time across eligible providers (README: "spread across
  providers"), **capping** a provider once its *known* count is reached. Unknown-capacity
  providers (Lambda, Nebius) are never capped by planning — they take their round-robin
  share and rely on execution-time failover if over-assigned.
- Returns `[(provider, n)]`. The sum may be `< count` only if *every* eligible provider has
  a finite known cap and their total is below the request — in which case the shortfall is
  surfaced immediately and rolled back.
- Satisfies README challenge #1 (query capacity then allocate) and #2 (partial failure →
  get rest from B/C), treating numbers as hints and relying on failover for correctness.

**Lambda specifics (rank, don't size).** Lambda capacity is boolean, and `launch` is atomic
all-or-nothing (`available < quantity → 400`, 0 created). So the scheduler can *rank* Lambda
but not *size* it. The fleet path sidesteps this by always creating with **`count=1`
(quantity=1 per launch)** — each VM is an independent success/failure, so Lambda partial-fills
naturally and atomicity only ever affects a single VM. (Layer-1 `vm create --count N` on
Lambda still uses the native atomic batch, honest to the real API.)

### Execution — one concurrent round, then failover rounds (concurrency + rollback)
```
plan = scheduler.allocate(gpu, count, providers)   # [(provider, n)]
provisioned = []
# Round 1: flatten to `count` single-VM tasks and fire EVERY provider concurrently.
tasks = [pname for pname, n in plan for _ in range(n)]
made, exhausted = run_creates_concurrently(gpu, tasks, name)   # one flat ThreadPool
provisioned += made
# Failover rounds: redistribute only the shortfall to providers not yet exhausted.
while len(provisioned) < count:
    candidates = [p for p in eligible if p.name not in exhausted]
    if not candidates: break
    tasks = distribute(count - len(provisioned), candidates)   # round-robin the gap
    made, more_exhausted = run_creates_concurrently(gpu, tasks, name)
    provisioned += made
    exhausted |= more_exhausted
    if not made and not (candidates_left := ...): break        # no progress → stop
if len(provisioned) < count:                                   # can't fulfill → ROLLBACK
    destroy_all_concurrently(provisioned)                      # tolerate reserved/un-destroyable
    raise FleetUnfulfilledError(...)
store.save(fleet_name, gpu, provisioned)                       # persist only on full success
```

- **Round 1 fires all providers at once** (flat `ThreadPoolExecutor` over `count` single-VM
  creates) — true cross-provider parallelism (README challenge #5), ~one create of
  wall-clock. `run_creates_concurrently` returns the successful `Instance`s and the set of
  provider names that returned `CapacityError` this round (i.e. exhausted).
- **Failover rounds** touch only the shortfall and skip exhausted providers; each ~3s.
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

1. Scaffolding: `models`, `errors` (provider-tagged), `logging`, `config`, `catalog`, `providers/base`, `output`, `cli` skeleton.
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
