"""Read uv.lock and compare it with the wheelhouse and the virtual environment.

Standard library only: used by check.py and fetch_wheels.py under /usr/bin/python3.
"""
import hashlib
import json
import os
import re
import subprocess
import tomllib


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_packages(lock_path):
    """Return {name: {"version": str, "wheels": [(filename, url, sha256), ...]}}.

    Only packages that come from an index are listed (not this repository itself).
    """
    with open(lock_path, "rb") as f:
        lock = tomllib.load(f)
    packages = {}
    for p in lock.get("package", []):
        if "registry" not in p.get("source", {}):
            continue
        wheels = []
        for w in p.get("wheels", []):
            url, digest = w["url"], w["hash"]
            if not digest.startswith("sha256:"):
                raise ValueError(f"{p['name']}: unsupported hash {digest!r}")
            wheels.append((url.rsplit("/", 1)[1], url, digest[len("sha256:"):]))
        packages[normalize(p["name"])] = {"version": p["version"], "wheels": wheels}
    return packages


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def wheelhouse_problems(lock_path, wheelhouse):
    """Every wheel named in the lock must be in the wheelhouse with the locked hash."""
    problems = []
    for name, p in sorted(locked_packages(lock_path).items()):
        if not p["wheels"]:
            problems.append(f"{name} {p['version']}: the lock lists no wheel")
        for filename, _url, digest in p["wheels"]:
            path = os.path.join(wheelhouse, filename)
            if not os.path.isfile(path):
                problems.append(f"{filename}: missing")
            elif sha256_file(path) != digest:
                problems.append(f"{filename}: SHA-256 differs from uv.lock")
    return problems


def venv_packages(venv_python):
    """Return {name: version} of what is installed in the virtual environment."""
    code = ("import importlib.metadata as m, json; "
            "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))")
    out = subprocess.run([venv_python, "-I", "-c", code], capture_output=True, text=True,
                         timeout=60, check=True).stdout
    return {normalize(k): v for k, v in json.loads(out).items()}


def venv_problems(lock_path, venv_python):
    """The environment must hold exactly the locked packages, at the locked versions."""
    locked = {n: p["version"] for n, p in locked_packages(lock_path).items()}
    installed = venv_packages(venv_python)
    problems = []
    for name in sorted(set(locked) | set(installed)):
        want, have = locked.get(name), installed.get(name)
        if want != have:
            problems.append(f"{name}: locked {want or 'no'}, installed {have or 'no'}")
    return problems
