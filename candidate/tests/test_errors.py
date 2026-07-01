from vm.errors import ProviderError, CapacityError, AuthError
from vm.models import State, Instance, CreateResult


def test_provider_error_carries_provider_and_str():
    e = CapacityError("nebius", "no capacity for h100.8x")
    assert e.provider == "nebius"
    assert isinstance(e, ProviderError)
    assert "nebius" in str(e) and "no capacity" in str(e)


def test_create_result_fulfilled_counts_successes():
    inst = Instance(id="i1", name="n", provider="lambda", gpu_type="h100.8x",
                    region="us-west-1", state=State.RUNNING, public_ip=None,
                    private_ip=None, reserved=False, reservation_id=None,
                    price_per_hour=27.68)
    r = CreateResult(requested=2, successes=[inst], errors=[AuthError("lambda", "bad key")])
    assert r.fulfilled == 1
