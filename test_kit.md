# Manual Test Kit — Unified GPU VM Manager

Run these by hand against the mock servers to verify the `candidate/` implementation.
Expected values are taken directly from the mock server seed data + capacity tables.

---

## 0. Setup

```bash
# 1. Start the three mock servers
cd mock_servers && bash start_all.sh    # Crusoe :8001  Lambda :8002  Nebius :50051

# 2. Point the kit at your CLI (whatever your entrypoint is)
export VM="python -m vm"        # or: export VM="./candidate/vm"  etc.

# 3. Sanity
$VM list
```

**Reset between full runs:** the mocks hold *in-memory* state (capacity decrements, reservations
get consumed, instances persist). To get the expected numbers below, **restart the servers**
(Ctrl-C `start_all.sh`, run it again) before a clean pass.

---

## 1. Ground truth — what "correct" looks like

### Seed instances (fresh `vm list` = **5 total**)

| Provider | Name | GPU (canonical) | Region | State | Reserved? | Notes |
|---|---|---|---|---|---|---|
| crusoe | gpu-worker-1 | a100.8x | us-west1 | RUNNING | on reservation rsv-001 | destroyable |
| lambda | training-node-1 | a100.8x | us-west-1 | RUNNING (active) | no | on-demand |
| lambda | dev-box | h100.1x | us-east-1 | RUNNING (active) | no | on-demand |
| lambda | **reserved-h100-node** | h100.8x | us-west-1 | RUNNING (active) | **YES** | **cannot be terminated** |
| nebius | train-node-01 | h100.8x | (none) | RUNNING | on reservation rsv-neb-001 | destroyable |

### On-demand capacity (nodes) — decrements on create, returns on destroy

| GPU | crusoe us-east1 / us-west1 / eu-west1 | lambda us-east-1 / us-west-1 / eu-west-1 | nebius (no region) |
|---|---|---|---|
| a100.1x | 5 / 10 / 3 | 5 / 10 / 3 | ❌ |
| a100.8x | 2 / 3 / 1 | 0 / 3 / 1 | ❌ |
| h100.1x | 8 / 4 / 2 | 8 / 5 / 2 | 6 |
| h100.8x | 1 / 2 / 0 | 1 / 2 / 0 | 2 |
| h200.1x | ❌ | ❌ | 4 |
| h200.8x | ❌ | ❌ | 1 |

### Reservations (extra capacity beyond on-demand)

| Provider | ID | GPU | Region | Free nodes |
|---|---|---|---|---|
| crusoe | rsv-002 | h100.8x | us-west1 | 4 |
| crusoe | rsv-001 | a100.8x | us-west1 | 1 |
| lambda | rsv-lambda-001 | h100.8x | us-west-1 | 1 (launching here makes VM non-terminable) |
| nebius | rsv-neb-001 | h100.8x | — | 2 |
| nebius | rsv-neb-002 | h200.8x | — | 2 |

### ⚠️ Region vocab differs per provider (normalization gotcha)

| Provider | Valid regions |
|---|---|
| crusoe | `us-east1` `us-west1` `eu-west1`  (no dash before digit) |
| lambda | `us-east-1` `us-west-1` `eu-west-1`  (dash before digit) |
| nebius | none — region is ignored |

If your CLI exposes a single `--region` vocabulary, it must map to the right per-provider string.

### GPU support matrix

| | a100.1x | a100.8x | h100.1x | h100.8x | h200.1x | h200.8x |
|---|---|---|---|---|---|---|
| crusoe | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| lambda | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| nebius | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |

---

## 2. Layer 1 checklist

> Async note: `create` on crusoe (~2.5s) and nebius (~3s) should **block until RUNNING**.
> Lambda is instant. If a create returns while state is still CREATING, that's a bug.
> Commands without `--json` (stop/start/destroy) — verify the result with `$VM get ... --json` or the raw inspectors in §4.

### list
```bash
$VM list                       # [ ] 5 instances, all 3 providers, states normalized (RUNNING)
$VM list --provider lambda     # [ ] exactly the 3 lambda rows above
$VM list --json                # [ ] valid JSON, canonical gpu_type + normalized state
```

### create → get → RUNNING
```bash
$VM create --provider crusoe --gpu h100.1x --region us-west1 --name t-cru   # [ ] RUNNING, took ~2.5s
$VM create --provider lambda --gpu h100.1x --region us-west-1 --name t-lam   # [ ] RUNNING, instant
$VM create --provider nebius --gpu h100.1x --name t-neb                      # [ ] RUNNING, took ~3s
$VM get <id> --provider crusoe --json                                       # [ ] matches what create returned
```

### stop / start
```bash
$VM stop  <crusoe_id> --provider crusoe    # [ ] converges to STOPPED (~2s), not stuck STOPPING
$VM start <crusoe_id> --provider crusoe    # [ ] back to RUNNING
$VM stop  <nebius_id> --provider nebius    # [ ] STOPPED
$VM start <nebius_id> --provider nebius    # [ ] RUNNING
$VM stop  <lambda_id> --provider lambda    # [ ] clean UnsupportedOperation error (lambda has no stop/start)
```

### destroy
```bash
$VM destroy <crusoe_id> --provider crusoe  # [ ] gone after ~2s; subsequent get => NotFound
$VM destroy <lambda_id> --provider lambda  # [ ] gone immediately (sync)
$VM destroy <nebius_id> --provider nebius  # [ ] gone after ~2s
```

### Edge cases
```bash
# Reserved lambda instance cannot be terminated
$VM list --provider lambda                  # find id of "reserved-h100-node"
$VM destroy <reserved_id> --provider lambda # [ ] ReservedInstanceError, VM stays; NOT a crash/traceback

# Unsupported GPU per provider — should fail BEFORE any network call, helpful message
$VM create --provider nebius --gpu a100.8x   # [ ] rejected (nebius has no a100)
$VM create --provider crusoe --gpu h200.8x   # [ ] rejected (crusoe has no h200)

# Capacity exhausted (deterministic: these regions have 0)
$VM create --provider crusoe --gpu h100.8x --region eu-west1   # [ ] CapacityError
$VM create --provider lambda --gpu h100.8x --region eu-west-1  # [ ] CapacityError

# Not found
$VM get does-not-exist --provider crusoe     # [ ] NotFoundError, clean message

# Auth (however your config overrides keys — env var, flag, etc.)
CRUSOE_KEY=bad $VM list --provider crusoe    # [ ] AuthError, non-zero exit, no traceback
```

---

## 3. Layer 2 — fleet checklist

```bash
# Small fleet — may land on a single provider, that's fine
$VM fleet create --gpu h100.1x --count 3 --name small
$VM fleet list                     # [ ] "small" listed
$VM fleet status small             # [ ] 3 VMs, all RUNNING, provider+state per VM, reconciled live
$VM fleet destroy small            # [ ] all torn down, removed from store

# Multi-provider span — no single provider trivially has this much on-demand
$VM fleet create --gpu h100.8x --count 6 --name big
$VM fleet status big               # [ ] 6 RUNNING; note the provider spread (capacity-aware = spreads)
                                   #     packing onto one provider is OK *if* all 6 are RUNNING
$VM fleet destroy big              # [ ] all gone

# ROLLBACK — unfulfillable request (only 3 h200.8x exist anywhere: 1 on-demand + 2 reserved on nebius)
$VM fleet create --gpu h200.8x --count 5    # [ ] FAILS clearly, does NOT half-create
$VM fleet list                              # [ ] no "…" fleet persisted
# cross-check nebius went back to just the 1 seed instance (§4), i.e. the 3 it made were rolled back
```

**Reserved-leak check:** after any fleet create/destroy on h100.8x, confirm the tool didn't
launch lambda VMs *into* `rsv-lambda-001` (those become non-terminable and would leak on rollback).
A good scheduler uses lambda **on-demand** only. Verify with §4 — no unexpected `is_reserved:true` VMs left behind.

---

## 4. Raw inspectors — cross-check CLI output vs. mock truth

Use these to confirm the CLI actually changed provider state (not just printed something).

```bash
# Lambda
curl -s localhost:8002/api/v1/instances \
  -H "Authorization: Bearer lambda-test-key-001" | python3 -m json.tool
curl -s localhost:8002/api/v1/instance-types \
  -H "Authorization: Bearer lambda-test-key-001" | python3 -m json.tool   # note: booleans, not counts

# Crusoe
curl -s localhost:8001/v1alpha5/projects/proj-001/compute/vms/instances \
  -H "Authorization: Bearer crusoe-test-key-001" | python3 -m json.tool
curl -s localhost:8001/v1alpha5/projects/proj-001/capacity \
  -H "Authorization: Bearer crusoe-test-key-001" | python3 -m json.tool

# Nebius (gRPC) — needs grpcurl WITH auth header (reflection is gated too)
grpcurl -plaintext -H "authorization: Bearer nebius-test-key-001" \
  -d '{"parent_id": "project-e1a2b3c4"}' \
  localhost:50051 nebius.compute.v1.InstanceService/List
```

---

## 5. Reset

```bash
# Ctrl-C the start_all.sh terminal, then:
cd mock_servers && bash start_all.sh
```

All in-memory state (instances, capacity, reservation usage) returns to the §1 ground truth.
