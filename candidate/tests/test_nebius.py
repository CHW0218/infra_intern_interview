import pytest
from vm.providers.nebius import NebiusProvider
from vm.models import State
from vm.errors import AuthError

pytestmark = pytest.mark.integration


def test_nebius_list_and_create(nebius_server):
    p = NebiusProvider()
    assert any(i.provider == "nebius" for i in p.list())
    res = p.create("h100.1x", count=1, name="neb-plan")
    assert res.fulfilled == 1
    inst = res.successes[0]
    assert inst.state == State.RUNNING
    assert inst.region is None  # Nebius has no region concept
    p.destroy(inst.id)


def test_nebius_capacity_unknown(nebius_server):
    assert NebiusProvider().capacity("h100.8x") is None


def test_nebius_auth_error(nebius_server):
    p = NebiusProvider(api_key="bad-key")
    with pytest.raises(AuthError):
        p.list()
