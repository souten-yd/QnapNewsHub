"""Offline comparison of two pinned llama-bench README revisions."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from qnapnewshub.document_blocks import blocks, compare, MAX_BYTES

SHAS = ("82e84610805065f1e2714cefa2f7f098904f2766",
        "3760072121150eab2c33e79a1a7be8b0fec71b57")


def verified(path, sha):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("source too large")
    data = path.read_bytes()
    if hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != sha:
        raise ValueError("source blob mismatch")
    return data.decode("utf-8")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("old", type=Path)
    p.add_argument("new", type=Path)
    args = p.parse_args()
    a, b = [verified(path, sha) for path, sha in zip((args.old, args.new), SHAS)]
    delta = compare(a, b)
    print(json.dumps({"old_blocks": len(blocks(a)), "new_blocks": len(blocks(b)),
                      **asdict(delta)}, indent=2))
