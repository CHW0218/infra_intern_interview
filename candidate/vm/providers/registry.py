from vm.providers.crusoe import CrusoeProvider
from vm.providers.lambda_ import LambdaProvider
from vm.providers.nebius import NebiusProvider

_FACTORIES = {"crusoe": CrusoeProvider, "lambda": LambdaProvider, "nebius": NebiusProvider}
NAMES = tuple(_FACTORIES.keys())


def get(name: str):
    if name not in _FACTORIES:
        raise ValueError(f"unknown provider '{name}' (known: {', '.join(NAMES)})")
    return _FACTORIES[name]()


def all():
    return [f() for f in _FACTORIES.values()]
