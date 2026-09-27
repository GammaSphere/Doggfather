"""Verify a Doggfather record offline.

    python -m doggfather.tools.verify_record record.json signing-key.pem

``record.json`` is what ``/records/<id>.json`` returns; the key is what
``/.well-known/doggfather/signing-key.pem`` returns. No network, no
database: only the canonical-JSON rule and Ed25519. Exit code 0 means
genuine; 1 means the signature does not match.

Revocation is not visible offline; ask the issuer's /verify page for that.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from doggfather.services.signing import key_id_for, load_public_key, verify


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("record", type=Path)
    parser.add_argument("public_key", type=Path)
    args = parser.parse_args(argv)

    data = json.loads(args.record.read_text(encoding="utf-8"))
    key = load_public_key(args.public_key.read_bytes())
    payload, signature = data["payload"], data["signature"]
    genuine = verify(key, payload, signature)
    key_matches = payload.get("key_id") in (None, key_id_for(key))
    print(f"record    {payload.get('record_id')}  ({payload.get('type')})")
    print(f"key id    {key_id_for(key)}{'' if key_matches else '  (payload names a different key)'}")
    print(f"signature {'VALID' if genuine and key_matches else 'INVALID'}")
    return 0 if genuine and key_matches else 1


if __name__ == "__main__":
    sys.exit(main())
