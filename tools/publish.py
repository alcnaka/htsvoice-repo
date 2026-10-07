#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import registry  # noqa: E402


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def exists(bucket: str, key: str, endpoint: str) -> bool:
    p = run("aws", "s3api", "head-object", "--bucket", bucket, "--key", key, "--endpoint-url", endpoint, check=False)
    return p.returncode == 0


def upload_file(bucket: str, key: str, path: Path, endpoint: str, cache_control: str, content_type: str) -> None:
    cmd = [
        "aws", "s3api", "put-object",
        "--bucket", bucket,
        "--key", key,
        "--body", str(path),
        "--endpoint-url", endpoint,
        "--cache-control", cache_control,
        "--content-type", content_type,
    ]
    run(*cmd)
    print(f"uploaded s3://{bucket}/{key}")


def required_keys(vdir: Path, voice: dict, rel: dict, lock_entry: dict) -> list[str]:
    keys: list[str] = []
    for var in rel["variants"]:
        h = lock_entry["variants"][var["id"]]["sha256"]
        keys.append(registry.blob_key(h, "htsvoice"))
    lh = lock_entry["license"]["sha256"]
    keys.append(registry.license_key(lh))
    if rel["source"].get("mirror", False):
        sh = lock_entry["source"]["sha256"]
        keys.append(registry.source_key(sh, rel["source"]["filename"]))
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish immutable artifacts and mutable metadata to R2")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--endpoint", required=True, help="R2 S3 endpoint, e.g. https://<account>.r2.cloudflarestorage.com")
    args = ap.parse_args()

    registry.validate_all(strict=True)
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        # Only fetch an upstream release if at least one immutable object is absent in R2.
        for vdir in registry.voice_dirs():
            voice = registry.load_voice(vdir)
            lock = registry.load_lock(vdir)
            for rel in voice["releases"]:
                if rel["status"] in {"pending", "withdrawn"}:
                    continue
                le = lock["releases"][rel["id"]]
                keys = required_keys(vdir, voice, rel, le)
                missing = [k for k in keys if not exists(args.bucket, k, args.endpoint)]
                if not missing:
                    print(f"skip upstream fetch; immutable objects already exist: {voice['id']}/{rel['id']}")
                    continue
                print(f"staging {voice['id']}/{rel['id']} because {len(missing)} object(s) are missing")
                files = registry.stage_release(voice["id"], rel["id"], stage)
                for p in files:
                    key = p.relative_to(stage).as_posix()
                    if exists(args.bucket, key, args.endpoint):
                        continue
                    if key.endswith(".htsvoice"):
                        ctype = "application/octet-stream"
                    elif key.endswith(".txt"):
                        ctype = "text/plain; charset=utf-8"
                    else:
                        ctype = "application/octet-stream"
                    upload_file(args.bucket, key, p, args.endpoint, "public, max-age=31536000, immutable", ctype)

        # Build metadata only after immutable objects are ensured.
        meta = stage / "meta"
        registry.build_metadata(meta)
        for p in sorted(meta.rglob("*.json")):
            key = p.relative_to(meta).as_posix()
            # index last so clients never observe metadata pointing at not-yet-published metadata files.
            if key == "v1/index.json":
                continue
            upload_file(args.bucket, key, p, args.endpoint, "public, max-age=300", "application/json; charset=utf-8")
        idx = meta / "v1/index.json"
        upload_file(args.bucket, "v1/index.json", idx, args.endpoint, "public, max-age=300", "application/json; charset=utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
