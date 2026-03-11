import json

import yaml

from main import app


def main() -> None:
    spec = app.openapi()

    with open("openapi.json", "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open("openapi.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(spec, f, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()

