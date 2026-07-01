from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATED_PATH = str(REPO_ROOT / "mock_servers" / "generated")


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    api_key: str
    scope: str          # project id / parent id ("" for Lambda)
    default_region: str | None   # CANONICAL region (see catalog); None = no region concept
    ssh_key: str


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


CRUSOE = ProviderConfig(
    base_url=_env("VM_CRUSOE_URL", "http://localhost:8001"),
    api_key=_env("VM_CRUSOE_KEY", "crusoe-test-key-001"),
    scope="proj-001", default_region="us-west", ssh_key="default-key",
)
LAMBDA = ProviderConfig(
    base_url=_env("VM_LAMBDA_URL", "http://localhost:8002"),
    api_key=_env("VM_LAMBDA_KEY", "lambda-test-key-001"),
    scope="", default_region="us-west", ssh_key="default-key",
)
NEBIUS = ProviderConfig(
    base_url=_env("VM_NEBIUS_URL", "localhost:50051"),
    api_key=_env("VM_NEBIUS_KEY", "nebius-test-key-001"),
    scope="project-e1a2b3c4", default_region=None, ssh_key="default-key",
)
