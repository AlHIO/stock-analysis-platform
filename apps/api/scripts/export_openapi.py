"""Generate/check the public contract without connecting to any database."""
import argparse
import json
from pathlib import Path

from stockapp.main import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / "openapi.json"
    content = json.dumps(create_app().openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.check:
        if not target.exists() or target.read_text() != content:
            raise SystemExit("OpenAPI contract changed; run apps/api/scripts/export_openapi.py")
        print("OpenAPI contract is current")
    else:
        target.write_text(content)
        print("Generated apps/api/openapi.json")


if __name__ == "__main__":
    main()
