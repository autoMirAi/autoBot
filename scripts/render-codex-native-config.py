from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_DIR / "deploy" / "codex-native-config.toml"
CATALOG_PATH = PROJECT_DIR / "deploy" / "deepseek-v4-flash.models.json"
RUNTIME_DIR = Path(os.environ.get("CODEX_HOME", "/run/autobot-codex"))


def fail(message: str) -> None:
    print(f"codex native config: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not credential_dir:
        fail("CREDENTIALS_DIRECTORY is not set")

    credential_path = Path(credential_dir) / "deepseek_api"
    try:
        api_key = credential_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        fail(f"cannot read systemd credential: {exc}")
    if not api_key:
        fail("DeepSeek API credential is empty")

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    models = catalog.get("models")
    if not isinstance(models, list) or [model.get("slug") for model in models] != ["deepseek-v4-flash"]:
        fail("model catalog must contain only deepseek-v4-flash")
    if "__DEEPSEEK_API_KEY__" not in template:
        fail("native provider template is missing API key placeholder")

    RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)
    config = template.replace("__DEEPSEEK_API_KEY__", json.dumps(api_key))
    config_path = RUNTIME_DIR / "config.toml"
    catalog_path = RUNTIME_DIR / "models.json"
    config_path.write_text(config, encoding="utf-8")
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(config_path, 0o600)
    os.chmod(catalog_path, 0o600)


if __name__ == "__main__":
    main()
