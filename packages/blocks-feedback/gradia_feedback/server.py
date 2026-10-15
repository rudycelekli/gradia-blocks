"""Run explicitly: python -m gradia_feedback.server. No default credentials."""
import json
import os
from pathlib import Path

from .api import create_app
from .contracts import AttachmentStorageSettings, BlockSettings, Principal


def configured_app():
    # File is deliberately external to the package; never commit credentials.
    path = os.environ.get("GRADIA_FEEDBACK_CREDENTIALS_FILE")
    database = os.environ.get("GRADIA_FEEDBACK_DATABASE")
    if not path or not database:
        raise RuntimeError("Set GRADIA_FEEDBACK_CREDENTIALS_FILE and GRADIA_FEEDBACK_DATABASE")
    credential_path = Path(path)
    if credential_path.stat().st_mode & 0o077:
        raise RuntimeError("Credential file must be readable only by its owner (chmod 600)")
    raw = json.loads(credential_path.read_text())
    keys = {key: Principal.model_validate(value) for key, value in raw.items()}
    config_path = os.environ.get("GRADIA_FEEDBACK_SETTINGS_FILE")
    settings = BlockSettings.model_validate_json(Path(config_path).read_text()) if config_path else BlockSettings()
    storage = AttachmentStorageSettings(backend=os.environ.get("GRADIA_FEEDBACK_ATTACHMENT_STORAGE", "sqlite"), directory=os.environ.get("GRADIA_FEEDBACK_ATTACHMENT_DIRECTORY"))
    return create_app(database, keys, settings, attachment_storage=storage)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(configured_app(), host="127.0.0.1", port=int(os.environ.get("GRADIA_FEEDBACK_PORT", "3191")))
