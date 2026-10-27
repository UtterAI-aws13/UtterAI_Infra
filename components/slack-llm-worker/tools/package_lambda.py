"""Build source + installed, locked pure-Python SDK into a Lambda archive without network."""
import argparse
import hashlib
from importlib.metadata import distribution, version
from pathlib import Path
import zipfile


def build(output):
    root = Path(__file__).resolve().parents[1]
    files = {}
    for line in (root / "requirements.lock").read_text().splitlines():
        name, expected = line.split("==")
        if version(name) != expected:
            raise RuntimeError(f"Version mismatch: {name}; use the locked build environment")
        dist = distribution(name)
        for f in dist.files or []:
            if ".." in f.parts or "__pycache__" in f.parts or f.suffix == ".pyc":
                continue
            path = Path(dist.locate_file(f))
            if path.is_file():
                files[str(f)] = path
    for p in (root / "slack_worker").glob("*.py"):
        files[str(p.relative_to(root))] = p
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return {"file": str(output), "files": len(files), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--output", default="dist/lambda.zip")
    print(build(parser.parse_args().output))
