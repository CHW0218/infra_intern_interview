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
