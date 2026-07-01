from __future__ import annotations
from vm.fleet.manager import FleetManager
from vm.errors import FleetUnfulfilledError


def _fleet_create(args) -> int:
    fm = FleetManager()
    try:
        rec = fm.create(args.gpu, count=args.count, name=args.name)
    except FleetUnfulfilledError as e:
        print(f"Fleet failed: {e} (rolled back {e.rolled_back}; "
              f"{len(e.undestroyable)} left reserved)")
        return 1
    print(f"Fleet '{rec.name}' created: {len(rec.vms)} VMs")
    for v in rec.vms:
        print(f"  {v['provider']:8s} {v['id'][:12]} {v['region'] or '-'}")
    return 0


def _fleet_list(args) -> int:
    fleets = FleetManager().list()
    if not fleets:
        print("(no fleets)")
        return 0
    for f in fleets:
        print(f"{f.name:16s} gpu={f.gpu:8s} vms={len(f.vms):3d} status={f.status}")
    return 0


def _fleet_status(args) -> int:
    st = FleetManager().status(args.name)
    print(f"Fleet '{st['name']}' gpu={st['gpu']} status={st['status']}")
    for v in st["vms"]:
        region = v.get("region") or "-"
        print(f"  {v['provider']:8s} {v['id'][:12]} {region:10s} {v['state']}")
    return 0


def _fleet_destroy(args) -> int:
    res = FleetManager().destroy(args.name)
    print(f"Destroyed {res['destroyed']} VMs; {len(res['left_reserved'])} left reserved")
    return 0


def attach(sub) -> None:
    fleet = sub.add_parser("fleet")
    fsub = fleet.add_subparsers(dest="fleet_command", required=True)

    c = fsub.add_parser("create")
    c.add_argument("--gpu", required=True)
    c.add_argument("--count", type=int, required=True)
    c.add_argument("--name", required=True)
    c.set_defaults(func=_fleet_create)

    fsub.add_parser("list").set_defaults(func=_fleet_list)

    s = fsub.add_parser("status")
    s.add_argument("name")
    s.set_defaults(func=_fleet_status)

    d = fsub.add_parser("destroy")
    d.add_argument("name")
    d.set_defaults(func=_fleet_destroy)
