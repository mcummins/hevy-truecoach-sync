#!/usr/bin/env python3
"""Hevy API helper — reads HEVY_API_KEY from .env itself so the key never
passes through Claude's context, a tool call, or the browser.

Usage:
  python3 hevy_api.py get  /v1/workouts?page=1&pageSize=1   [--out FILE]
  python3 hevy_api.py put  /v1/routines/<id>  --body FILE    [--out FILE]
  python3 hevy_api.py post /v1/routines       --body FILE    [--out FILE]
  python3 hevy_api.py folder [--folder-id 2355979] [--out FILE]   # all routines in folder -> [{id,title}]
  python3 hevy_api.py check                                    # auth probe, prints status only

Without --out: prints JSON {"status": <http>, "body": <parsed json or text>}.
With --out: writes the raw response BODY only (the exact JSON Hevy returned —
directly usable by `bidirectional_sync.py validate-put --response` and, for
`folder`, as hevy_folder.json) and prints just {"status", "out"}. Exit 0 on 2xx, 1 otherwise. Never prints the key.
"""
import argparse, json, sys, urllib.request, urllib.error
from pathlib import Path

BASE = "https://api.hevyapp.com"
HERE = Path(__file__).resolve().parent


def _key():
    for line in (HERE / ".env").read_text().splitlines():
        if line.strip().startswith("HEVY_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("HEVY_API_KEY not found in .env")


def call(method, path, body=None):
    url = path if path.startswith("http") else BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper(),
                                 headers={"api-key": _key(), "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode(errors="replace")
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw
    return status, parsed


def folder_routines(folder_id):
    out, page, pc = [], 1, 1
    while page <= pc:
        s, b = call("GET", f"/v1/routines?page={page}&pageSize=10")
        if s != 200:
            return s, b
        pc = b.get("page_count", 1)
        out += [{"id": r["id"], "title": r["title"]} for r in b.get("routines", [])
                if r.get("folder_id") == folder_id]
        page += 1
    return 200, {"routines": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["get", "put", "post", "folder", "check"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--body")
    ap.add_argument("--out")
    ap.add_argument("--folder-id", type=int, default=2355979)
    a = ap.parse_args()

    if a.cmd == "check":
        s, _ = call("GET", "/v1/workouts?page=1&pageSize=1")
        print(json.dumps({"status": s}))
        sys.exit(0 if s == 200 else 1)
    if a.cmd == "folder":
        s, b = folder_routines(a.folder_id)
    else:
        body = json.loads(Path(a.body).read_text()) if a.body else None
        s, b = call(a.cmd, a.path, body)
    res = {"status": s, "body": b}
    if a.out:
        Path(a.out).write_text(json.dumps(b, indent=1))
        print(json.dumps({"status": s, "out": a.out}))
    else:
        print(json.dumps(res))
    sys.exit(0 if 200 <= s < 300 else 1)


if __name__ == "__main__":
    main()
