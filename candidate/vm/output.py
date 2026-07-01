import json
from dataclasses import asdict
from vm.models import Instance


def _table(rows: list[list[str]], headers: list[str]) -> str:
    cols = list(zip(*([headers] + rows))) if rows else [[h] for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]

    def line(r):
        return "  ".join(str(c).ljust(w) for c, w in zip(r, widths))

    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def render_instances(instances: list[Instance], as_json: bool = False) -> str:
    if as_json:
        return json.dumps([{**asdict(i), "state": i.state.value} for i in instances], indent=2)
    if not instances:
        return "(no instances)"
    headers = ["PROVIDER", "ID", "NAME", "GPU", "REGION", "STATE", "PUBLIC_IP", "RSVD"]
    rows = [[i.provider, i.id[:12], i.name, i.gpu_type, i.region or "-",
             i.state.value, i.public_ip or "-", "yes" if i.reserved else "no"] for i in instances]
    return _table(rows, headers)
