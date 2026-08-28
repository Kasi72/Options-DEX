import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reference_hashes_match_manifest() -> None:
    root = Path(__file__).parents[2]
    manifest = json.loads((root / "reference/manifest.json").read_text())

    assert manifest["files"] == [
        {
            "source_path": "C:\\Users\\drkkr\\Downloads\\app.py",
            "copied_path": "reference/original_app.py",
            "sha256": "4C7DC443C60321C452E615CC2FB904C2DF9C68E1BE044D617239A49DDB5C8C63",
        },
        {
            "source_path": "C:\\Users\\drkkr\\Downloads\\nifty_vol_gex_dex_log.csv",
            "copied_path": "reference/nifty_vol_gex_dex_log.csv",
            "sha256": "E9D828EB4FF44506097BF7C2D3FD63A161E3DC4EFE499FFB5898957D6EB5F1A0",
        },
    ]

    for item in manifest["files"]:
        assert sha256(root / item["copied_path"]).upper() == item["sha256"]
