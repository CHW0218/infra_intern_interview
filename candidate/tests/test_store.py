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
