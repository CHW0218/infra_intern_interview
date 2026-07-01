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
