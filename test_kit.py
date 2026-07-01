#!/usr/bin/env python3
"""
Automated smoke test for the unified `vm` CLI (Layer 1 + Layer 2).

It DRIVES your CLI and VERIFIES the effect against the live mock servers, so it does
not depend on your CLI's output format. Real instance IDs are obtained by diffing mock
state before/after each action.

Usage:
    cd mock_servers && bash start_all.sh          # in one terminal
    export VM="python -m vm"                       # <- your CLI entrypoint
    python3 test_kit.py                            # run this with a python that has the
                                                   #   mock_servers deps (grpcio), i.e. the
                                                   #   same env you run the servers with.

Exit code = number of failed checks (0 = all good). SKIPs don't count as failures.
Re-runnable: it creates uniquely-named resources and tears them down. To reset the mocks'
in-memory capacity completely, restart start_all.sh.
"""

import os, sys, json, time, shlex, subprocess, contextlib, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))

# --- config (matches mock servers / docs) ---
VM         = os.environ.get("VM")
CRUSOE     = "http://localhost:8001"; CRUSOE_KEY = "crusoe-test-key-001"; PROJ = "proj-001"
LAMBDA     = "http://localhost:8002"; LAMBDA_KEY = "lambda-test-key-001"
NEBIUS_ADDR= "localhost:50051";       NEBIUS_KEY = "nebius-test-key-001"; PARENT = "project-e1a2b3c4"

LAMBDA_TO_CANON = {"gpu_1x_a100": "a100.1x", "gpu_8x_a100": "a100.8x",
                   "gpu_1x_h100": "h100.1x", "gpu_8x_h100": "h100.8x"}
NEBIUS_TO_CANON = {("gpu-h100-sxm", "1gpu-16vcpu-200gb"): "h100.1x",
                   ("gpu-h100-sxm", "8gpu-160vcpu-1600gb"): "h100.8x",
                   ("gpu-h200-sxm", "1gpu-20vcpu-256gb"): "h200.1x",
                   ("gpu-h200-sxm", "8gpu-160vcpu-2048gb"): "h200.8x"}
NEB_STATE = {0: "UNKNOWN", 1: "CREATING", 2: "RUNNING", 3: "STOPPING",
             4: "STOPPED", 5: "STARTING", 6: "DELETING", 7: "ERROR"}

# ------------------------------------------------------------------- output helpers
_TTY = sys.stdout.isatty()
def _c(s, col):
    if not _TTY: return s
    return {"green": "\033[32m", "red": "\033[31m", "yellow": "\033[33m",
            "bold": "\033[1m", "dim": "\033[2m"}[col] + s + "\033[0m"
def tail(s, n=200):
    s = (s or "").strip().replace("\n", " ⏎ ")
    return s[-n:]

PASS = FAIL = SKIP = 0
FAILURES = []
class Skip(Exception): pass

@contextlib.contextmanager
def T(name):
    global PASS, FAIL, SKIP
    ctx = {"note": ""}
    try:
        yield ctx
    except Skip as e:
        SKIP += 1; print(f"  {_c('SKIP', 'yellow')} {name} — {e}")
    except AssertionError as e:
        FAIL += 1; FAILURES.append(name); print(f"  {_c('FAIL', 'red')} {name} — {e}")
    except Exception as e:
        FAIL += 1; FAILURES.append(name); print(f"  {_c('FAIL', 'red')} {name} — unexpected: {e!r}")
    else:
        PASS += 1
        note = f" — {_c(ctx['note'], 'dim')}" if ctx["note"] else ""
        print(f"  {_c('PASS', 'green')} {name}{note}")

# ------------------------------------------------------------------- CLI driver
def vm(*args, timeout=45):
    cmd = shlex.split(VM) + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or ""), (e.stderr or "") + f"\n[timeout after {timeout}s]"
    except FileNotFoundError as e:
        return 127, "", f"cannot exec {cmd[0]}: {e}"

# ------------------------------------------------------------------- REST inspectors
def _http(method, url, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    if data: req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode())
        except Exception: return e.code, {}
    except Exception as e:
        return None, {"_err": str(e)}

def crusoe_list():
    _, b = _http("GET", f"{CRUSOE}/v1alpha5/projects/{PROJ}/compute/vms/instances", CRUSOE_KEY)
    return b.get("items", [])
def crusoe_delete(iid):
    _http("DELETE", f"{CRUSOE}/v1alpha5/projects/{PROJ}/compute/vms/instances/{iid}", CRUSOE_KEY)
def lambda_list():
    _, b = _http("GET", f"{LAMBDA}/api/v1/instances", LAMBDA_KEY)
    return b.get("data", [])
def lambda_terminate(iid):
    _http("POST", f"{LAMBDA}/api/v1/instance-operations/terminate", LAMBDA_KEY, {"instance_ids": [iid]})

# ------------------------------------------------------------------- Nebius (gRPC) inspector
NEBIUS_IMPORT = False
try:
    import grpc as _grpc
    sys.path.insert(0, os.path.join(HERE, "mock_servers", "generated"))
    from nebius.compute.v1 import instance_service_pb2 as _nsvc, instance_service_pb2_grpc as _ngrpc
    _nstub = _ngrpc.InstanceServiceStub(_grpc.insecure_channel(NEBIUS_ADDR))
    _NMETA = [("authorization", f"Bearer {NEBIUS_KEY}")]
    NEBIUS_IMPORT = True
except Exception as _e:
    _nimport_err = str(_e)

def nebius_list():
    resp = _nstub.List(_nsvc.ListInstancesRequest(parent_id=PARENT), metadata=_NMETA, timeout=10)
    out = {}
    for i in resp.instances:
        out[i.metadata.id] = {
            "name": i.metadata.name,
            "state": NEB_STATE.get(i.status.state, "UNKNOWN"),
            "gpu": NEBIUS_TO_CANON.get((i.spec.resources.platform, i.spec.resources.preset), "?"),
            "reserved": bool(i.status.reservation_id),
        }
    return out
def nebius_delete(iid):
    _nstub.Delete(_nsvc.DeleteInstanceRequest(id=iid), metadata=_NMETA, timeout=10)

# ------------------------------------------------------------------- normalized view
def instances_of(p):
    """provider -> {id: {name,state,gpu,reserved}} straight from the mock (ground truth)."""
    try:
        if p == "crusoe":
            return {it["id"]: {"name": it["name"], "state": it["state"].replace("STATE_", ""),
                               "gpu": it["type"], "reserved": it.get("billing_type") == "reserved"}
                    for it in crusoe_list()}
        if p == "lambda":
            return {it["id"]: {"name": it["name"],
                               "state": "RUNNING" if it["status"] == "active" else it["status"].upper(),
                               "gpu": LAMBDA_TO_CANON.get(it["instance_type"]["name"], it["instance_type"]["name"]),
                               "reserved": bool(it.get("is_reserved"))}
                    for it in lambda_list()}
        if p == "nebius":
            return nebius_list()
    except Exception:
        return {}
    return {}

def snapshot(p): return set(instances_of(p).keys())
def created_since(p, before): return {i: v for i, v in instances_of(p).items() if i not in before}
def state_of(p, iid): return instances_of(p).get(iid, {}).get("state")

def wait_for(pred, timeout=15, interval=0.4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred(): return True
        except Exception:
            pass
        time.sleep(interval)
    return False

# ------------------------------------------------------------------- bookkeeping
CREATED = set()   # (provider, id) to clean up
IDS = {}          # provider -> id created in the L1 lifecycle
NAMES = {}        # provider -> name created
UNIQ = str(int(time.time()))[-6:]
FLEET = f"smoke-{UNIQ}"
ROLLBK = f"rollbk-{UNIQ}"
FLEET_IDS = []

REACH = {}

def preflight():
    print(_c("Preflight — mock server reachability:", "bold"))
    REACH["crusoe"] = _http("GET", f"{CRUSOE}/v1alpha5/projects/{PROJ}/compute/vms/instances", CRUSOE_KEY)[0] == 200
    REACH["lambda"] = _http("GET", f"{LAMBDA}/api/v1/instances", LAMBDA_KEY)[0] == 200
    REACH["nebius"] = False
    if NEBIUS_IMPORT:
        try:
            nebius_list(); REACH["nebius"] = True
        except Exception as e:
            print(f"    nebius gRPC error: {e}")
    else:
        print(f"    nebius stubs unavailable ({_nimport_err}) — run with the server's python")
    for p in ("crusoe", "lambda", "nebius"):
        print(f"    {p:7} {'UP' if REACH[p] else _c('DOWN', 'red')}")
    if not VM:
        print(_c("\nERROR: set VM to your CLI, e.g.  export VM=\"python -m vm\"", "red")); sys.exit(2)
    if not any(REACH.values()):
        print(_c("\nERROR: no mock servers reachable. Start them: cd mock_servers && bash start_all.sh", "red")); sys.exit(2)
    print(f"\nUsing VM = {_c(VM, 'bold')}\n")

def providers_up():  return [p for p in ("crusoe", "lambda", "nebius") if REACH.get(p)]
def supports_stopstart(p): return p in ("crusoe", "nebius")

# ------------------------------------------------------------------- tests
def t_list():
    print(_c("Layer 1 — list", "bold"))
    with T("list (all providers) exits 0 and shows instances") as ctx:
        rc, out, err = vm("list")
        assert rc == 0, f"exit {rc}: {tail(err or out)}"
        names = [v["name"] for p in providers_up() for v in instances_of(p).values()]
        hit = [n for n in names if n and n in out]
        assert hit, "no known instance names/ids found in output"
        ctx["note"] = f"found {len(hit)}/{len(names)} known instances in output"
    if REACH.get("lambda") and REACH.get("crusoe"):
        with T("list --provider lambda filters correctly"):
            rc, out, err = vm("list", "--provider", "lambda")
            assert rc == 0, f"exit {rc}: {tail(err or out)}"
            cru = [v["name"] for v in instances_of("crusoe").values()]
            leaked = [n for n in cru if n and n in out]
            assert not leaked, f"crusoe instances leaked into lambda-filtered list: {leaked}"
    with T("list --json is valid JSON (optional)") as ctx:
        rc, out, err = vm("list", "--json")
        if rc != 0: raise Skip("--json not supported / errored")
        try: json.loads(out)
        except Exception: raise AssertionError("--json output is not parseable JSON")

def t_lifecycle(p):
    gpu = "h100.1x"  # available in every region on all three providers
    with T(f"create → RUNNING: {p}") as ctx:
        before = snapshot(p)
        nm = f"life-{p}-{UNIQ}"
        t0 = time.monotonic()
        rc, out, err = vm("create", "--provider", p, "--gpu", gpu, "--count", "1", "--name", nm)
        assert rc == 0, f"exit {rc}: {tail(err or out)}"
        assert wait_for(lambda: len(created_since(p, before)) >= 1, 12), "no new instance appeared in provider"
        new = created_since(p, before)
        assert len(new) == 1, f"expected 1 new instance, got {len(new)}"
        iid = next(iter(new)); IDS[p] = iid; NAMES[p] = nm; CREATED.add((p, iid))
        blocked = state_of(p, iid) == "RUNNING"
        assert wait_for(lambda: state_of(p, iid) == "RUNNING", 12), f"never reached RUNNING (last={state_of(p, iid)})"
        assert instances_of(p)[iid]["gpu"] == gpu, f"wrong gpu recorded: {instances_of(p)[iid]['gpu']}"
        dt = time.monotonic() - t0
        ctx["note"] = f"{iid[:8]} RUNNING in ~{dt:.1f}s ({'CLI blocked' if blocked else 'CLI returned before RUNNING'})"

    with T(f"get: {p}"):
        iid = IDS.get(p)
        if not iid: raise Skip("create failed")
        rc, out, err = vm("get", iid, "--provider", p)
        assert rc == 0, f"exit {rc}: {tail(err or out)}"
        assert iid in out or NAMES.get(p, "") in out, "get output doesn't reference the instance id or name"

    if supports_stopstart(p):
        with T(f"stop → STOPPED: {p}"):
            iid = IDS.get(p)
            if not iid: raise Skip("create failed")
            rc, out, err = vm("stop", iid, "--provider", p)
            assert rc == 0, f"exit {rc}: {tail(err or out)}"
            assert wait_for(lambda: state_of(p, iid) == "STOPPED", 12), f"never STOPPED (last={state_of(p, iid)})"
        with T(f"start → RUNNING: {p}"):
            iid = IDS.get(p)
            if not iid: raise Skip("create failed")
            rc, out, err = vm("start", iid, "--provider", p)
            assert rc == 0, f"exit {rc}: {tail(err or out)}"
            assert wait_for(lambda: state_of(p, iid) == "RUNNING", 12), f"never RUNNING (last={state_of(p, iid)})"
    else:
        with T(f"stop is rejected as unsupported: {p}") as ctx:
            iid = IDS.get(p)
            if not iid: raise Skip("create failed")
            rc, out, err = vm("stop", iid, "--provider", p)
            assert rc != 0, "expected non-zero exit (lambda has no stop/start)"
            assert iid in instances_of(p), "instance disappeared after an unsupported op"
            ctx["note"] = "clean UnsupportedOperation error"

    with T(f"destroy → gone: {p}"):
        iid = IDS.get(p)
        if not iid: raise Skip("create failed")
        rc, out, err = vm("destroy", iid, "--provider", p)
        assert rc == 0, f"exit {rc}: {tail(err or out)}"
        assert wait_for(lambda: iid not in instances_of(p), 12), "still present after destroy"
        CREATED.discard((p, iid))

def t_reserved():
    print(_c("Error handling & constraints", "bold"))
    with T("reserved lambda instance cannot be terminated") as ctx:
        if not REACH.get("lambda"): raise Skip("lambda down")
        res = [i for i, v in instances_of("lambda").items() if v["reserved"]]
        if not res: raise Skip("no reserved lambda seed present (restart mocks)")
        iid = res[0]
        rc, out, err = vm("destroy", iid, "--provider", "lambda")
        assert rc != 0, "expected error terminating reserved instance"
        assert iid in instances_of("lambda"), "reserved instance was actually terminated!"
        ctx["note"] = "rejected, instance preserved"

def t_unsupported_gpu():
    for p, gpu in (("nebius", "a100.8x"), ("crusoe", "h200.8x")):
        with T(f"unsupported GPU rejected: {p} {gpu}"):
            if not REACH.get(p): raise Skip(f"{p} down")
            before = snapshot(p)
            rc, out, err = vm("create", "--provider", p, "--gpu", gpu, "--count", "1", "--name", f"bad-{UNIQ}")
            new = created_since(p, before)
            for iid in new: (crusoe_delete if p == "crusoe" else nebius_delete)(iid)  # cleanup if wrongly created
            assert rc != 0, f"expected rejection, got exit 0"
            assert not new, "an instance was created despite unsupported GPU"

def t_capacity():
    with T("capacity error: crusoe h100.8x in eu-west (0 available)") as ctx:
        if not REACH.get("crusoe"): raise Skip("crusoe down")
        region = os.environ.get("EU_WEST", "eu-west")  # candidate uses canonical region tokens
        before = snapshot("crusoe")
        rc, out, err = vm("create", "--provider", "crusoe", "--gpu", "h100.8x",
                          "--region", region, "--count", "1", "--name", f"cap-{UNIQ}")
        new = created_since("crusoe", before)
        for iid in new: crusoe_delete(iid)  # cleanup
        assert rc != 0, "expected capacity error"
        assert not new, "instance created despite zero capacity (region ignored / rerouted?)"
        ctx["note"] = "CapacityError as expected"

def t_not_found():
    with T("not found: get bogus id"):
        p = "crusoe" if REACH.get("crusoe") else ("lambda" if REACH.get("lambda") else None)
        if not p: raise Skip("no REST provider up")
        rc, out, err = vm("get", "00000000-0000-0000-0000-000000000000", "--provider", p)
        assert rc != 0, "expected non-zero exit for missing instance"

def t_fleet():
    print(_c("Layer 2 — fleet", "bold"))
    up = providers_up()
    base = {p: snapshot(p) for p in up}
    with T("fleet create (6× h100.8x, spread across providers)") as ctx:
        rc, out, err = vm("fleet", "create", "--gpu", "h100.8x", "--count", "6", "--name", FLEET, timeout=120)
        assert rc == 0, f"exit {rc}: {tail(err or out)}"
        def total(): return sum(len(created_since(p, base[p])) for p in up)
        assert wait_for(lambda: total() >= 6, 25), f"fewer than 6 VMs appeared (got {total()})"
        allids = [(p, i) for p in up for i in created_since(p, base[p])]
        for t in allids: CREATED.add(t)
        assert wait_for(lambda: all(state_of(p, i) == "RUNNING" for p, i in allids), 25), "not all fleet VMs reached RUNNING"
        assert len(allids) == 6, f"expected exactly 6 VMs, got {len(allids)}"
        FLEET_IDS[:] = allids
        provs = sorted({p for p, _ in allids})
        ctx["note"] = f"6 RUNNING across {provs}"

    with T("fleet list shows the fleet"):
        rc, out, err = vm("fleet", "list")
        assert rc == 0, f"exit {rc}: {tail(err or out)}"
        assert FLEET in out, "fleet name not in `fleet list` output"

    with T("fleet status reconciles live state"):
        if not FLEET_IDS: raise Skip("fleet create failed")
        rc, out, err = vm("fleet", "status", FLEET)
        assert rc == 0, f"exit {rc}: {tail(err or out)}"
        assert FLEET in out or any(i in out for _, i in FLEET_IDS), "status doesn't reference the fleet/VMs"

    with T("fleet destroy tears down every VM"):
        if not FLEET_IDS: raise Skip("fleet create failed")
        rc, out, err = vm("fleet", "destroy", FLEET, timeout=90)
        assert rc == 0, f"exit {rc}: {tail(err or out)}"
        assert wait_for(lambda: all(i not in instances_of(p) for p, i in FLEET_IDS), 25), "some fleet VMs remain after destroy"
        for t in FLEET_IDS: CREATED.discard(t)

def t_fleet_rollback():
    with T("fleet rollback: unfulfillable 5× h200.8x → no leaks, not persisted") as ctx:
        if not REACH.get("nebius"): raise Skip("nebius down (h200 only exists on nebius)")
        base = snapshot("nebius")   # h200.8x only exists on nebius; total capacity = 3 (< 5)
        rc, out, err = vm("fleet", "create", "--gpu", "h200.8x", "--count", "5", "--name", ROLLBK, timeout=120)
        settled = wait_for(lambda: snapshot("nebius") == base, 25)  # rollback destroys are async
        leaked = created_since("nebius", base)
        for iid in leaked: nebius_delete(iid)   # cleanup any leak
        assert settled and not leaked, f"{len(leaked)} VM(s) leaked — rollback did not clean up"
        _, lout, _ = vm("fleet", "list")
        assert ROLLBK not in lout, "an unfulfillable fleet was persisted to the store"
        ctx["note"] = "failed cleanly, rolled back, nothing persisted"

def cleanup():
    # best-effort teardown of anything we created via the mocks directly
    stray = [t for t in CREATED]
    for p, iid in stray:
        try:
            if p == "crusoe": crusoe_delete(iid)
            elif p == "lambda": lambda_terminate(iid)
            elif p == "nebius": nebius_delete(iid)
        except Exception:
            pass
    for name in (FLEET, ROLLBK):
        vm("fleet", "destroy", name, timeout=30)  # ignore result

# ------------------------------------------------------------------- main
def main():
    print(_c("=== Unified vm CLI — automated smoke test ===\n", "bold"))
    preflight()
    try:
        t_list()
        print()
        for p in providers_up():
            print(_c(f"Layer 1 — lifecycle: {p}", "bold"))
            t_lifecycle(p)
            print()
        t_reserved()
        t_unsupported_gpu()
        t_capacity()
        t_not_found()
        print()
        t_fleet()
        print()
        t_fleet_rollback()
    finally:
        print()
        print(_c("Cleaning up any leftovers…", "dim"))
        cleanup()

    print()
    print(_c("=" * 48, "bold"))
    print(f"  {_c('PASS', 'green')} {PASS}    {_c('FAIL', 'red')} {FAIL}    {_c('SKIP', 'yellow')} {SKIP}")
    if FAILURES:
        print("  failed checks:")
        for f in FAILURES:
            print(f"    - {f}")
    print(_c("=" * 48, "bold"))
    sys.exit(FAIL)

if __name__ == "__main__":
    main()
