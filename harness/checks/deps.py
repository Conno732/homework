"""Dependency resolution shared by the dependency checks.

The resolved tree comes from a fixture (a stand-in for ``mvn dependency:tree``
/ pip resolution) overlaid with what the manifests in the repo actually say,
so a stale fixture can't hide a real pin.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .common import PRContext, Unknown


@dataclass
class PackageChange:
    service: str
    ecosystem: str
    package: str
    base_version: str | None
    head_version: str | None
    relationship: str  # direct | transitive | parent_managed | unknown (as of base)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pip_lock(text: str) -> dict:
    pins = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;#]+)", line.strip())
        if m:
            pins[m.group(1)] = m.group(2)
    return pins


def parse_pom(text: str):
    """Return (properties, {group:artifact -> explicit version})."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise Unknown(f"pom.xml does not parse: {exc}") from exc
    for el in root.iter():
        el.tag = el.tag.split("}", 1)[-1]
    props_el = root.find("properties")
    props = {c.tag: (c.text or "").strip() for c in (props_el if props_el is not None else [])}
    deps = {}
    for path in ("dependencies/dependency", "dependencyManagement/dependencies/dependency"):
        for dep in root.findall(path):
            group, artifact, version = (dep.findtext(t) for t in ("groupId", "artifactId", "version"))
            if group and artifact and version:
                version = version.strip()
                if (m := re.fullmatch(r"\$\{(.+)\}", version)):
                    version = props.get(m.group(1), version)
                deps[f"{group.strip()}:{artifact.strip()}"] = version
    return props, deps


def resolved(ctx: PRContext, service: str, side: str) -> dict:
    """{package: {"version", "relationship"}} for a service at base or head."""
    key = ("resolved", service, side)
    if key in ctx.cache:
        return ctx.cache[key]
    svc = ctx.data("dependency_trees").get(service)
    if not svc:
        raise Unknown(f"no dependency tree for service '{service}'")
    pkgs = {name: dict(info) for name, info in svc["packages"].items()}
    manifest = f"services/{service}/{svc['manifest']}"
    text = ctx.read(manifest, side)
    if text is None:
        raise Unknown(f"{manifest} is missing at {side}")

    if svc["ecosystem"] == "pip":
        pins = parse_pip_lock(text)
        by_canon = {canonical(n): n for n in pkgs}
        seen = set()
        for name, version in pins.items():
            k = by_canon.get(canonical(name), name)
            pkgs.setdefault(k, {"relationship": "unknown"})["version"] = version
            seen.add(k)
        for name in [n for n in pkgs if n not in seen]:
            del pkgs[name]  # no longer pinned
    elif svc["ecosystem"] == "maven":
        props, deps = parse_pom(text)
        for name, info in list(pkgs.items()):
            prop = info.get("version_property")
            if prop and prop in props:
                info["version"] = props[prop]
            if info.get("declared_in_pom") and name not in deps:
                del pkgs[name]  # dependency removed from the pom
        for name, version in deps.items():
            pkgs.setdefault(name, {"relationship": "unknown"})["version"] = version
    else:
        raise Unknown(f"unsupported ecosystem '{svc['ecosystem']}'")

    for info in pkgs.values():
        info["ecosystem"] = svc["ecosystem"]
    ctx.cache[key] = pkgs
    return pkgs


def changed_packages(ctx: PRContext) -> list:
    """Packages whose resolved version differs between base and head."""
    if "changed_packages" in ctx.cache:
        return ctx.cache["changed_packages"]
    out = []
    manifests = [p for p in ctx.changed_paths if ctx.file_class(p) == "manifest"]
    for path in manifests:
        if ctx.service_of(path) is None:
            raise Unknown(f"{path}: manifest outside a known service")
    for service in sorted({ctx.service_of(p) for p in manifests}):
        base, head = resolved(ctx, service, "base"), resolved(ctx, service, "head")
        for name in sorted(set(base) | set(head)):
            b, h = base.get(name), head.get(name)
            if (b or {}).get("version") != (h or {}).get("version"):
                out.append(PackageChange(
                    service=service,
                    ecosystem=(b or h)["ecosystem"],
                    package=name,
                    base_version=b["version"] if b else None,
                    head_version=h["version"] if h else None,
                    relationship=(b or h).get("relationship", "unknown"),
                ))
    ctx.cache["changed_packages"] = out
    return out


# --------------------------------------------------------------------------- versions

def version_tuple(version: str) -> tuple:
    nums = []
    for part in re.split(r"[.\-+]", version):
        m = re.match(r"\d+", part)
        if not m:
            break
        nums.append(int(m.group()))
    if not nums:
        raise Unknown(f"cannot parse version '{version}'")
    return tuple(nums)


def _cmp(a: tuple, b: tuple) -> int:
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def in_range(version: str, spec: str) -> bool:
    """spec is comma-separated comparators, e.g. '< 5.4' or '>= 1.0, <= 1.33'."""
    v = version_tuple(version)
    for clause in spec.split(","):
        m = re.fullmatch(r"\s*(<=|>=|<|>|=)\s*(\S+)\s*", clause)
        if not m:
            raise Unknown(f"cannot parse version range '{spec}'")
        c = _cmp(v, version_tuple(m.group(2)))
        ok = {"<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0, "=": c == 0}[m.group(1)]
        if not ok:
            return False
    return True


def known_vulnerabilities(ctx: PRContext, ecosystem: str, package: str, version: str) -> list:
    return [
        v for v in ctx.data("vulndb")["advisories"]
        if v["ecosystem"] == ecosystem
        and canonical(v["package"]) == canonical(package)
        and in_range(version, v["vulnerable_range"])
    ]
