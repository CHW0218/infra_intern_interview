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
