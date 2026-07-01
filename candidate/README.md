# Unified GPU Cloud VM Manager

A single CLI (`vm`) that manages GPU VMs across **Crusoe** (REST, async), **Lambda**
(REST, sync), and **Nebius** (gRPC, async) behind one clean abstraction, plus a **fleet
manager** that schedules across providers with concurrency, failover, and rollback.

Design spec: [`../docs/superpowers/specs/2026-07-01-vm-manager-design.md`](../docs/superpowers/specs/2026-07-01-vm-manager-design.md)
· Plan: [`../docs/superpowers/plans/2026-07-01-vm-manager.md`](../docs/superpowers/plans/2026-07-01-vm-manager.md)

## Setup

```bash
# 1. Start the mock cloud servers (in a separate shell)
cd mock_servers && bash start_all.sh

# 2. Install the CLI deps (reuses the mock servers' venv)
mock_servers/.venv/bin/pip install httpx

# 3. Run the CLI from the candidate/ dir
cd candidate
../mock_servers/.venv/bin/python -m vm list
```

(No proto compilation needed — the Nebius gRPC stubs in `mock_servers/generated/` are used
directly via `sys.path`.)

## Layer 1 — unified single-VM commands

```bash
vm list [--provider crusoe|lambda|nebius] [--json]
vm create --provider <p> --gpu <type> --count <n> [--name <name>] [--region <region>] [--json]
vm get <id> --provider <p> [--json]
vm stop <id> --provider <p>          # unsupported on Lambda (clear error)
vm start <id> --provider <p>         # unsupported on Lambda
vm destroy <id> --provider <p>       # reserved Lambda instances cannot be terminated
```

- **GPU types** (canonical): `a100.1x a100.8x h100.1x h100.8x h200.1x h200.8x`
  (Nebius only offers `h100.*`/`h200.*`; unsupported combos fail before any network call).
- **Regions** (canonical): `us-west us-east eu-west` — normalized per provider
  (Crusoe `us-westN`, Lambda `us-west-N`, Nebius has none).
- Every error names its provider: `Error [crusoe]: no capacity for h100.8x in eu-west`.

## Layer 2 — fleet manager

```bash
vm fleet create --gpu <type> --count <n> --name <fleet_name>
vm fleet list
vm fleet status <fleet_name>
vm fleet destroy <fleet_name>
```

- **Cross-provider scheduling**: queries capacity (best-effort), spreads round-robin across
  eligible providers, capped by known capacity.
- **Concurrent**: fires every provider's creates in one parallel round; failover rounds
  handle only the shortfall.
- **Rollback**: if the request can't be fully satisfied, all created VMs are torn down
  (tolerating Lambda reserved instances that can't be terminated).
- **State**: persisted to `~/.vmfleet/fleets.json`; `status` reconciles against live
  provider state (flags `MISSING` for drift).

## Architecture

```
vm/
  cli.py            argparse surface + top-level error handling (provider-tagged)
  models.py         normalized Instance / CapacitySlot / CreateResult / State
  errors.py         ProviderError hierarchy (each carries .provider)
  catalog.py        canonical GPU + region <-> per-provider mapping, support matrix
  config.py         endpoints, keys, scope, defaults, generated-stub path
  output.py         table + --json rendering
  providers/
    base.py         Provider ABC + Capabilities + poll-until-state helper
    lambda_.py      sync REST
    crusoe.py       async REST, project-scoped, region auto-select
    nebius.py       gRPC, poll Get() for completion (no operation RPC)
    registry.py     name -> provider
  fleet/
    store.py        JSON persistence (atomic writes)
    scheduler.py    round-robin allocation, capacity-capped
    manager.py      concurrent create / failover / rollback / status / destroy
  fleet_cli.py      fleet subcommands
```

The key abstraction choice: providers are unified where they agree and **explicit where they
differ** — capability flags (`supports_stop_start`, `native_batch`, `queryable_capacity`),
`UnsupportedOperationError` as a first-class result, and best-effort `capacity()` that may
return "unknown". Correctness under limited/unknown capacity comes from
**attempt → failover → rollback**, not from trusting capacity numbers.

## Tests

```bash
cd candidate
# unit (no servers)
../mock_servers/.venv/bin/python -m pytest -q -m "not integration"
# integration (needs the mock servers running)
../mock_servers/.venv/bin/python -m pytest -q -m integration
```
