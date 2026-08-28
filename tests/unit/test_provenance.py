import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reference_hashes_match_manifest() -> None:
    root = Path(__file__).parents[2]
    manifest = json.loads((root / "reference/manifest.json").read_text())
    for item in manifest["files"]:
        assert sha256(root / item["copied_path"]).upper() == item["sha256"]
