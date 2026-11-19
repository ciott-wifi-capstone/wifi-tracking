"""Register one application-image digest for the boards in a node registry."""
from argparse import ArgumentParser
from hashlib import sha256
from pathlib import Path
import json


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    args = parser.parse_args()
    registry = json.loads(args.registry.read_text(encoding="utf-8-sig"))
    nodes = registry.get("nodes")
    if not isinstance(nodes, dict) or set(nodes) != {f"A{i}" for i in range(7)}:
        parser.error("Registry must contain nodes A0 through A6")
    digest = sha256(args.image.read_bytes()).hexdigest()
    registry["expected_firmware_sha256"] = digest
    for node in nodes.values():
        node.setdefault("firmware", {})["application_sha256"] = digest
    temporary = args.registry.with_suffix(args.registry.suffix + ".tmp")
    temporary.write_text(json.dumps(registry, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(args.registry)
    print(f"Registered application SHA-256 {digest}")


if __name__ == "__main__":
    main()
