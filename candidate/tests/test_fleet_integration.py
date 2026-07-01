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
