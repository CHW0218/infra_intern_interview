import pytest
from vm import catalog
from vm.errors import UnsupportedOperationError, InvalidArgumentError


def test_to_native_per_provider():
    assert catalog.to_native("crusoe", "h100.8x") == {"type": "h100.8x"}
    assert catalog.to_native("lambda", "h100.8x") == {"instance_type_name": "gpu_8x_h100"}
    assert catalog.to_native("nebius", "h100.8x") == {
        "platform": "gpu-h100-sxm", "preset": "8gpu-160vcpu-1600gb"}


def test_nebius_rejects_a100():
    assert not catalog.supports("nebius", "a100.8x")
    with pytest.raises(UnsupportedOperationError):
        catalog.to_native("nebius", "a100.8x")


def test_providers_for_h200_is_nebius_only():
    assert catalog.providers_for("h200.8x") == ["nebius"]


def test_unknown_gpu_raises():
    with pytest.raises(InvalidArgumentError):
        catalog.to_native("crusoe", "v100.1x")


def test_region_round_trip():
    assert catalog.region_to_native("crusoe", "us-west") == "us-west1"
    assert catalog.region_to_native("lambda", "us-west") == "us-west-1"
    assert catalog.region_to_native("nebius", "us-west") is None
    assert catalog.region_to_canonical("lambda", "us-west-1") == "us-west"
    assert catalog.region_to_canonical("crusoe", "us-west1") == "us-west"


def test_unknown_region_raises():
    with pytest.raises(InvalidArgumentError):
        catalog.region_to_native("crusoe", "mars-1")
