from __future__ import annotations
import argparse
import sys

from vm import output
from vm.logging_setup import configure, get_logger
from vm.errors import ProviderError
from vm.providers import registry

log = get_logger("vm")


def _cmd_list(args) -> int:
    providers = [registry.get(args.provider)] if args.provider else registry.all()
    instances = []
    for p in providers:
        try:
            instances += p.list()
        except ProviderError as e:
            log.error("[%s] list failed: %s", e.provider, e.message)
    print(output.render_instances(instances, args.json))
    return 0


def _cmd_create(args) -> int:
    p = registry.get(args.provider)
    res = p.create(args.gpu, count=args.count, name=args.name, region=args.region)
    for e in res.errors:
        log.error("[%s] create failed: %s", e.provider, e.message)
    print(output.render_instances(res.successes, args.json))
    return 0 if res.fulfilled == res.requested else 1


def _cmd_get(args) -> int:
    print(output.render_instances([registry.get(args.provider).get(args.id)], args.json))
    return 0


def _cmd_stop(args) -> int:
    inst = registry.get(args.provider).stop(args.id)
    print(f"{inst.id} -> {inst.state.value}")
    return 0


def _cmd_start(args) -> int:
    inst = registry.get(args.provider).start(args.id)
    print(f"{inst.id} -> {inst.state.value}")
    return 0


def _cmd_destroy(args) -> int:
    registry.get(args.provider).destroy(args.id)
    print(f"{args.id} destroyed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vm", description="Unified GPU VM manager")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--provider", choices=registry.NAMES)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_create = sub.add_parser("create")
    p_create.add_argument("--provider", required=True, choices=registry.NAMES)
    p_create.add_argument("--gpu", required=True)
    p_create.add_argument("--count", type=int, default=1)
    p_create.add_argument("--name")
    p_create.add_argument("--region")
    p_create.add_argument("--json", action="store_true")
    p_create.set_defaults(func=_cmd_create)

    for cmd, func in [("get", _cmd_get), ("stop", _cmd_stop), ("start", _cmd_start), ("destroy", _cmd_destroy)]:
        pc = sub.add_parser(cmd)
        pc.add_argument("id")
        pc.add_argument("--provider", required=True, choices=registry.NAMES)
        pc.add_argument("--json", action="store_true")
        pc.set_defaults(func=func)

    from vm import fleet_cli
    fleet_cli.attach(sub)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure(getattr(args, "verbose", False))
    try:
        return args.func(args)
    except ProviderError as e:
        print(f"Error [{e.provider}]: {e.message}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
