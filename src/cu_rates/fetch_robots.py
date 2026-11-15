"""Conservative robots.txt gate for the initial plain HTTP collector."""

from dataclasses import dataclass

PRODUCT = "CU-Rates.org"


@dataclass(frozen=True)
class RobotsPolicy:
    disallowed: tuple[str, ...] = ()
    delay: int = 0

    def permits(self, path: str) -> bool:
        return not any(path.startswith(prefix) for prefix in self.disallowed)


def parse_robots(body: bytes) -> RobotsPolicy:
    # Deliberately deny when the relevant group uses syntax this small parser
    # cannot verify. Never let an Allow override a matching Disallow in V2's
    # first worker; this is conservative and not a full RFC 9309 interpreter.
    delay = 0
    denied: list[str] = []
    agents: list[str] = []
    started_rules = False
    for raw in body.decode("utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError("unrecognized robots line")
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not value:
                raise ValueError("empty robots agent")
            if started_rules:
                agents.clear()
                started_rules = False
            agents.append(value.lower())
            continue
        if key not in {"allow", "disallow", "crawl-delay"}:
            continue
        if not agents:
            raise ValueError("robots rule outside an agent group")
        started_rules = True
        if not any(agent == "*" or PRODUCT.lower().startswith(agent) for agent in agents):
            continue
        if key == "crawl-delay":
            if not value.isascii() or not value.isdecimal() or len(value) > 6:
                raise ValueError("unsupported robots delay")
            delay = max(delay, int(value))
        elif key == "disallow" and value:
            if not value.startswith("/") or any(c in value for c in "*$%"):
                raise ValueError("unsupported robots pattern")
            denied.append(value)
    return RobotsPolicy(tuple(denied), delay)
