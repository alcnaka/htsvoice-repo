#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VOICES_DIR = ROOT / "voices"
CACHE_DIR = ROOT / ".cache"
LOCK_NAME = "lock.json"


class RegistryError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def load_registry() -> dict[str, Any]:
    return load_toml(ROOT / "registry.toml")


def voice_dirs() -> list[Path]:
    return sorted(p.parent for p in VOICES_DIR.glob("*/voice.toml"))


def load_voice(vdir: Path) -> dict[str, Any]:
    return load_toml(vdir / "voice.toml")


def load_lock(vdir: Path) -> dict[str, Any]:
    p = vdir / LOCK_NAME
    if not p.exists():
        return {"schema_version": 1, "releases": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save_lock(vdir: Path, lock: dict[str, Any]) -> None:
    json_dump(vdir / LOCK_NAME, lock)


def release_map(voice: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in voice.get("releases", [])}


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise RegistryError(message)


def validate_voice(vdir: Path, strict: bool = False) -> list[str]:
    warnings: list[str] = []
    voice = load_voice(vdir)
    lock = load_lock(vdir)
    vid = voice.get("id")
    _require(voice.get("schema_version") == 1, f"{vdir.name}: schema_version must be 1")
    _require(vid == vdir.name, f"{vdir.name}: id must match directory name")
    for key in ("name", "description", "language", "locale", "publisher"):
        _require(bool(voice.get(key)), f"{vid}: missing {key}")
    lic = voice.get("license", {})
    for key in ("spdx", "name", "url"):
        _require(bool(lic.get(key)), f"{vid}: missing license.{key}")
    releases = voice.get("releases", [])
    _require(releases, f"{vid}: at least one release is required")
    ids: set[str] = set()
    for rel in releases:
        rid = rel.get("id")
        _require(rid and rid not in ids, f"{vid}: duplicate or empty release id")
        ids.add(rid)
        _require(isinstance(rel.get("revision"), int) and rel["revision"] >= 1, f"{vid}/{rid}: invalid revision")
        _require(rel.get("status") in {"pending", "current", "superseded", "deprecated", "withdrawn"}, f"{vid}/{rid}: invalid status")
        src = rel.get("source", {})
        _require(src.get("kind") in {"zip", "tar.gz"}, f"{vid}/{rid}: source.kind must be zip or tar.gz")
        for key in ("url", "homepage", "distribution", "filename", "license_path"):
            _require(bool(src.get(key)), f"{vid}/{rid}: missing source.{key}")
        expected_sha256 = src.get("expected_sha256")
        if expected_sha256 is not None:
            _require(
                isinstance(expected_sha256, str) and len(expected_sha256) == 64
                and all(c in "0123456789abcdef" for c in expected_sha256),
                f"{vid}/{rid}: invalid source.expected_sha256",
            )
        git_commit = src.get("git_commit")
        if git_commit is not None:
            _require(
                isinstance(git_commit, str) and len(git_commit) == 40
                and all(c in "0123456789abcdef" for c in git_commit),
                f"{vid}/{rid}: invalid source.git_commit",
            )
        variants = rel.get("variants", [])
        _require(variants, f"{vid}/{rid}: at least one variant is required")
        variant_ids: set[str] = set()
        for var in variants:
            _require(var.get("id") and var["id"] not in variant_ids, f"{vid}/{rid}: duplicate/empty variant id")
            variant_ids.add(var["id"])
            _require(bool(var.get("path")), f"{vid}/{rid}/{var.get('id')}: missing path")
        locked = lock.get("releases", {}).get(rid)
        if rel["status"] != "pending" and not locked:
            msg = f"{vid}/{rid}: published release has no {LOCK_NAME} entry"
            if strict:
                raise RegistryError(msg)
            warnings.append(msg)
        if locked:
            _require(len(locked.get("source", {}).get("sha256", "")) == 64, f"{vid}/{rid}: invalid locked source sha256")
            for var in variants:
                item = locked.get("variants", {}).get(var["id"])
                _require(item is not None, f"{vid}/{rid}: lock missing variant {var['id']}")
                _require(len(item.get("sha256", "")) == 64, f"{vid}/{rid}/{var['id']}: invalid sha256")
    return warnings


def validate_all(strict: bool = False) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for vdir in voice_dirs():
        voice = load_voice(vdir)
        vid = voice.get("id")
        _require(vid not in seen, f"duplicate voice id: {vid}")
        seen.add(vid)
        warnings.extend(validate_voice(vdir, strict=strict))
    return warnings


def download(url: str, *, use_cache: bool = True) -> bytes:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_path = CACHE_DIR / key
    if use_cache and cache_path.exists():
        return cache_path.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "htsvoice-repo/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    except (urllib.error.URLError, TimeoutError) as e:
        raise RegistryError(f"download failed: {url}: {e}") from e
    if use_cache:
        cache_path.write_bytes(data)
    return data


def _normalized_member(name: str, strip_components: int) -> str:
    p = PurePosixPath(name)
    parts = [x for x in p.parts if x not in ("", ".")]
    if ".." in parts:
        raise RegistryError(f"unsafe archive member: {name}")
    if strip_components > len(parts):
        return ""
    return str(PurePosixPath(*parts[strip_components:]))


def archive_member(data: bytes, kind: str, wanted: str, strip_components: int = 0) -> bytes:
    wanted = str(PurePosixPath(wanted))
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if _normalized_member(info.filename, strip_components) == wanted:
                    return zf.read(info)
    elif kind == "tar.gz":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile() and _normalized_member(member.name, strip_components) == wanted:
                    f = tf.extractfile(member)
                    if f is None:
                        break
                    return f.read()
    raise RegistryError(f"archive member not found: {wanted}")


def inspect_htsvoice(data: bytes) -> dict[str, Any]:
    marker = data.find(b"[DATA]")
    if marker < 0:
        return {}
    head = data[:marker].decode("utf-8", errors="replace")
    raw: dict[str, str] = {}
    for line in head.splitlines():
        if ":" not in line or line.startswith("["):
            continue
        k, v = line.split(":", 1)
        raw[k.strip()] = v.strip()
    fields = {
        "HTS_VOICE_VERSION": "hts_voice_version",
        "SAMPLING_FREQUENCY": "sampling_frequency",
        "FRAME_PERIOD": "frame_period",
        "NUM_STATES": "num_states",
        "NUM_STREAMS": "num_streams",
        "STREAM_TYPE": "stream_type",
    }
    out: dict[str, Any] = {}
    for src, dst in fields.items():
        if src not in raw:
            continue
        val: Any = raw[src]
        if src in {"SAMPLING_FREQUENCY", "FRAME_PERIOD", "NUM_STATES", "NUM_STREAMS"}:
            try:
                val = int(val)
            except ValueError:
                pass
        out[dst] = val
    return out


@dataclass
class FetchedRelease:
    source: bytes
    license_bytes: bytes
    variants: dict[str, bytes]


def fetch_release(voice: dict[str, Any], rel: dict[str, Any], *, use_cache: bool = True) -> FetchedRelease:
    src = rel["source"]
    source = download(src["url"], use_cache=use_cache)
    expected = src.get("expected_sha256")
    if expected and sha256_bytes(source) != expected:
        raise RegistryError(
            f"{voice['id']}/{rel['id']}: source SHA-256 does not match source.expected_sha256 "
            f"({sha256_bytes(source)} != {expected})"
        )
    strip = int(src.get("strip_components", 0))
    variants: dict[str, bytes] = {}
    for var in rel["variants"]:
        variants[var["id"]] = archive_member(source, src["kind"], var["path"], strip)
    license_bytes = archive_member(source, src["kind"], src["license_path"], strip)
    return FetchedRelease(source=source, license_bytes=license_bytes, variants=variants)


def make_lock_entry(rel: dict[str, Any], fetched: FetchedRelease) -> dict[str, Any]:
    return {
        "source": {
            "sha256": sha256_bytes(fetched.source),
            "size": len(fetched.source),
        },
        "license": {
            "sha256": sha256_bytes(fetched.license_bytes),
            "size": len(fetched.license_bytes),
        },
        "variants": {
            vid: {
                "sha256": sha256_bytes(data),
                "size": len(data),
                "hts": inspect_htsvoice(data),
            }
            for vid, data in fetched.variants.items()
        },
    }


def lock_release(voice_id: str, release_id: str) -> None:
    vdir = VOICES_DIR / voice_id
    _require((vdir / "voice.toml").exists(), f"unknown voice: {voice_id}")
    voice = load_voice(vdir)
    rel = release_map(voice).get(release_id)
    _require(rel is not None, f"unknown release: {voice_id}/{release_id}")
    fetched = fetch_release(voice, rel)
    entry = make_lock_entry(rel, fetched)
    lock = load_lock(vdir)
    old = lock.setdefault("releases", {}).get(release_id)
    if old and old != entry:
        raise RegistryError(
            f"{voice_id}/{release_id}: upstream content differs from existing lock; "
            "create a new +rN release instead of overwriting the lock"
        )
    lock["releases"][release_id] = entry
    save_lock(vdir, lock)
    print(f"locked {voice_id}/{release_id}: {entry['source']['sha256']}")


def verify_fetched_against_lock(voice_id: str, rel: dict[str, Any], lock_entry: dict[str, Any], fetched: FetchedRelease) -> None:
    actual = make_lock_entry(rel, fetched)
    if actual != lock_entry:
        raise RegistryError(
            f"{voice_id}/{rel['id']}: upstream changed. Existing release is immutable; "
            "add a new revision and lock that revision."
        )


def blob_key(hash_: str, suffix: str) -> str:
    return f"v1/blobs/sha256/{hash_[:2]}/{hash_}.{suffix}"


def source_key(hash_: str, filename: str) -> str:
    return f"v1/sources/sha256/{hash_[:2]}/{hash_}/{filename}"


def license_key(hash_: str) -> str:
    return f"v1/licenses/sha256/{hash_[:2]}/{hash_}.txt"


def release_json(reg: dict[str, Any], voice: dict[str, Any], rel: dict[str, Any], lock_entry: dict[str, Any]) -> dict[str, Any]:
    base = reg["base_url"].rstrip("/")
    api = reg.get("api_prefix", "/v1").rstrip("/")
    src_lock = lock_entry["source"]
    lic_lock = lock_entry["license"]
    out_vars = []
    for var in rel["variants"]:
        vlock = lock_entry["variants"][var["id"]]
        out_vars.append({
            "id": var["id"],
            "name": var.get("name", var["id"]),
            "style": var.get("style"),
            "artifact": {
                "sha256": vlock["sha256"],
                "size": vlock["size"],
                "url": f"{base}/{blob_key(vlock['sha256'], 'htsvoice')}",
            },
            "hts": vlock.get("hts", {}),
        })
    source_url = None
    if rel["source"].get("mirror", False):
        source_url = f"{base}/{source_key(src_lock['sha256'], rel['source']['filename'])}"
    return {
        "schema_version": 1,
        "voice_id": voice["id"],
        "release": rel["id"],
        "upstream_version": rel["upstream_version"],
        "revision": rel["revision"],
        "status": rel["status"],
        "observed_at": rel.get("observed_at"),
        "source": {
            "homepage": rel["source"]["homepage"],
            "distribution": rel["source"]["distribution"],
            "upstream_url": rel["source"]["url"],
            "filename": rel["source"]["filename"],
            "sha256": src_lock["sha256"],
            "size": src_lock["size"],
            "mirror_url": source_url,
            "git_commit": rel["source"].get("git_commit"),
        },
        "license": {
            **voice["license"],
            "sha256": lic_lock["sha256"],
            "url": f"{base}/{license_key(lic_lock['sha256'])}",
        },
        "attribution": voice.get("attribution", {}),
        "variants": out_vars,
    }


def build_metadata(out_dir: Path, include_pending: bool = False) -> None:
    warnings = validate_all(strict=False)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    reg = load_registry()
    base = reg["base_url"].rstrip("/")
    api = reg.get("api_prefix", "/v1").rstrip("/")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "v1" / "voices").mkdir(parents=True, exist_ok=True)
    (out_dir / "v1" / "releases").mkdir(parents=True, exist_ok=True)
    voices_index = []
    for vdir in voice_dirs():
        voice = load_voice(vdir)
        lock = load_lock(vdir)
        release_entries = []
        current: str | None = None
        for rel in voice["releases"]:
            lock_entry = lock.get("releases", {}).get(rel["id"])
            if not lock_entry:
                if include_pending:
                    release_entries.append({
                        "id": rel["id"], "status": rel["status"], "locked": False
                    })
                continue
            rjson = release_json(reg, voice, rel, lock_entry)
            rpath = out_dir / "v1" / "releases" / voice["id"] / f"{rel['id']}.json"
            json_dump(rpath, rjson)
            release_entries.append({
                "id": rel["id"],
                "upstream_version": rel["upstream_version"],
                "revision": rel["revision"],
                "status": rel["status"],
                "locked": True,
                "metadata": f"{base}{api}/releases/{voice['id']}/{rel['id']}.json",
            })
            if rel["status"] == "current":
                _require(current is None, f"{voice['id']}: multiple current releases")
                current = rel["id"]
        vjson = {
            "schema_version": 1,
            "id": voice["id"],
            "name": voice["name"],
            "description": voice["description"],
            "language": voice["language"],
            "locale": voice["locale"],
            "publisher": voice["publisher"],
            "license": voice["license"],
            "attribution": voice.get("attribution", {}),
            "current": current,
            "releases": release_entries,
        }
        json_dump(out_dir / "v1" / "voices" / f"{voice['id']}.json", vjson)
        voices_index.append({
            "id": voice["id"],
            "name": voice["name"],
            "locale": voice["locale"],
            "license": voice["license"]["spdx"],
            "current": current,
            "metadata": f"{base}{api}/voices/{voice['id']}.json",
        })
    generated_at = os.getenv("SOURCE_DATE_EPOCH")
    if generated_at:
        ts = datetime.fromtimestamp(int(generated_at), tz=timezone.utc)
    else:
        ts = datetime.now(timezone.utc)
    index = {
        "schema_version": 1,
        "name": reg["name"],
        "generated_at": ts.isoformat().replace("+00:00", "Z"),
        "voices": voices_index,
    }
    json_dump(out_dir / "v1" / "index.json", index)
    print(f"metadata built at {out_dir}")


def stage_release(voice_id: str, release_id: str, out_dir: Path) -> list[Path]:
    vdir = VOICES_DIR / voice_id
    voice = load_voice(vdir)
    rel = release_map(voice).get(release_id)
    _require(rel is not None, f"unknown release: {voice_id}/{release_id}")
    lock_entry = load_lock(vdir).get("releases", {}).get(release_id)
    _require(lock_entry is not None, f"{voice_id}/{release_id}: not locked; run lock first")
    fetched = fetch_release(voice, rel)
    verify_fetched_against_lock(voice_id, rel, lock_entry, fetched)
    written: list[Path] = []
    for var in rel["variants"]:
        data = fetched.variants[var["id"]]
        h = sha256_bytes(data)
        p = out_dir / blob_key(h, "htsvoice")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        written.append(p)
    lh = sha256_bytes(fetched.license_bytes)
    lp = out_dir / license_key(lh)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_bytes(fetched.license_bytes)
    written.append(lp)
    if rel["source"].get("mirror", False):
        sh = sha256_bytes(fetched.source)
        sp = out_dir / source_key(sh, rel["source"]["filename"])
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_bytes(fetched.source)
        written.append(sp)
    return written


def check_upstream(voice_id: str, release_id: str) -> int:
    vdir = VOICES_DIR / voice_id
    voice = load_voice(vdir)
    rel = release_map(voice).get(release_id)
    _require(rel is not None, f"unknown release: {voice_id}/{release_id}")
    locked = load_lock(vdir).get("releases", {}).get(release_id)
    _require(locked is not None, f"{voice_id}/{release_id}: not locked")
    try:
        fetched = fetch_release(voice, rel, use_cache=False)
    except RegistryError as e:
        print(json.dumps({"voice": voice_id, "release": release_id, "status": "unavailable", "error": str(e)}, ensure_ascii=False))
        return 2
    actual = make_lock_entry(rel, fetched)
    if actual == locked:
        print(json.dumps({"voice": voice_id, "release": release_id, "status": "unchanged", "source_sha256": actual["source"]["sha256"]}))
        return 0
    print(json.dumps({
        "voice": voice_id,
        "release": release_id,
        "status": "changed",
        "locked_source_sha256": locked["source"]["sha256"],
        "actual_source_sha256": actual["source"]["sha256"],
    }))
    return 3


def main() -> int:
    p = argparse.ArgumentParser(description="Build and validate htsvoice-repo")
    sub = p.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("validate")
    pv.add_argument("--strict", action="store_true", help="require locks for non-pending releases")
    pl = sub.add_parser("lock")
    pl.add_argument("voice")
    pl.add_argument("release")
    pb = sub.add_parser("build-metadata")
    pb.add_argument("--out", default=str(ROOT / "dist"))
    pb.add_argument("--include-pending", action="store_true")
    ps = sub.add_parser("stage-release")
    ps.add_argument("voice")
    ps.add_argument("release")
    ps.add_argument("--out", default=str(ROOT / "dist"))
    pc = sub.add_parser("check-upstream")
    pc.add_argument("voice")
    pc.add_argument("release")
    args = p.parse_args()
    try:
        if args.cmd == "validate":
            warnings = validate_all(strict=args.strict)
            for w in warnings:
                print(f"warning: {w}")
            print(f"validated {len(voice_dirs())} voices")
        elif args.cmd == "lock":
            lock_release(args.voice, args.release)
        elif args.cmd == "build-metadata":
            build_metadata(Path(args.out), include_pending=args.include_pending)
        elif args.cmd == "stage-release":
            files = stage_release(args.voice, args.release, Path(args.out))
            for f in files:
                print(f)
        elif args.cmd == "check-upstream":
            return check_upstream(args.voice, args.release)
    except RegistryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
