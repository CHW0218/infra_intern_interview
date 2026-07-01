import logging
import sys

_CONFIGURED = False


def configure(verbose: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    # Quiet noisy third-party request loggers (httpx logs every call at INFO)
    if not verbose:
        for noisy in ("httpx", "httpcore", "grpc"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
