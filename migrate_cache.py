"""Explicit, backed-up repair of the known year/day suffix; never sends messages."""

import argparse
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

from reliability import atomic_json
from scraper import announcement_identity, canonical_surat, normalize_surat


def migrate(path, *, apply=False):
    path = Path(path)
    original = path.read_bytes()
    records = json.loads(original)
    if not isinstance(records, list) or any(not isinstance(i, dict) for i in records):
        raise ValueError("Expected a list of cache records")
    repaired = deepcopy(records)
    count = 0
    for before, after in zip(records, repaired):
        corrected = canonical_surat(before)
        if corrected != normalize_surat(before.get("surat", "")):
            after["surat"] = corrected
            count += 1
        if announcement_identity(before) != announcement_identity(after):
            raise ValueError("Migration changed detection identity")
    if apply and count:
        digest = hashlib.sha256(original).hexdigest()
        backup = path.with_name(path.name + "." + digest[:16] + ".bak")
        if backup.exists():
            if backup.read_bytes() != original:
                raise ValueError("Existing backup does not match input")
        else:
            with backup.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        if hashlib.sha256(backup.read_bytes()).hexdigest() != digest:
            raise ValueError("Backup verification failed")
        if path.read_bytes() != original:
            raise ValueError("Cache changed during migration")
        atomic_json(path, repaired)
        if json.loads(path.read_bytes()) != repaired:
            raise ValueError("Post-migration verification failed; restore backup")
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(f"Matching repairs: {migrate(args.path, apply=args.apply)}; apply={args.apply}")
