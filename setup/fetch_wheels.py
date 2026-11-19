#!/usr/bin/python3
"""Download every wheel named in uv.lock into wheelhouse/ (needs internet).

Each file is checked against the SHA-256 in uv.lock before it is kept. Run it
after any change to uv.lock; setup/build_venv.sh then works without internet.

    setup/fetch_wheels.py
"""
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uvlock  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    lock = os.path.join(REPO, "uv.lock")
    wheelhouse = os.path.join(REPO, "wheelhouse")
    os.makedirs(wheelhouse, exist_ok=True)
    wanted = set()
    for name, p in sorted(uvlock.locked_packages(lock).items()):
        for filename, url, digest in p["wheels"]:
            wanted.add(filename)
            path = os.path.join(wheelhouse, filename)
            if os.path.isfile(path) and uvlock.sha256_file(path) == digest:
                print(f"have     {filename}")
                continue
            tmp = path + ".part"
            request = urllib.request.Request(url, headers={"User-Agent": "fetch_wheels"})
            with urllib.request.urlopen(request, timeout=60) as r, open(tmp, "wb") as f:
                while block := r.read(1 << 20):
                    f.write(block)
            if uvlock.sha256_file(tmp) != digest:
                os.remove(tmp)
                print(f"ERROR: {filename}: downloaded file does not match the SHA-256 in uv.lock")
                return 1
            os.replace(tmp, path)
            print(f"fetched  {filename}")
    for extra in sorted(set(os.listdir(wheelhouse)) - wanted):
        print(f"note: {extra} is in wheelhouse/ but not in uv.lock (safe to delete)")
    problems = uvlock.wheelhouse_problems(lock, wheelhouse)
    for p in problems:
        print("ERROR:", p)
    print(f"wheelhouse: {len(wanted)} wheels" + (" complete" if not problems else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
