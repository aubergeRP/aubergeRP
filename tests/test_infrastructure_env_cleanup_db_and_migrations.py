"""
Infrastructure tests for packaging, environment overrides, media cleanup,
SQLite persistence, and migrations.

Covers:
  1. Docker packaging files exist
  2. Environment-variable config overrides
  3. Media cleanup endpoint + scheduler helper
  4. SQLite database storage (character + conversation via DB)
  5. Migration system (built-in + custom)
"""
from __future__ import annotations

import json
import os
import time
from datetime import UTC
from pathlib import Path

from fastapi.testclient import TestClient

from aubergeRP.config import load_config, reset_config
from aubergeRP.database import init_db
from aubergeRP.models.character import CharacterData
from aubergeRP.scheduler import cleanup_images
from aubergeRP.services.character_service import CharacterService
from aubergeRP.services.conversation_service import ConversationService

# ---------------------------------------------------------------------------
# 1. Docker packaging files
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent


def test_dockerfile_exists():
    assert (_REPO_ROOT / "Dockerfile").exists()


def test_dockerfile_uses_slim_image():
    content = (_REPO_ROOT / "Dockerfile").read_text()
    assert "python:" in content and "slim" in content


def test_dockerfile_non_root_user():
    content = (_REPO_ROOT / "Dockerfile").read_text()
    assert "useradd" in content or "adduser" in content


def test_docker_compose_exists():
    assert (_REPO_ROOT / "docker/docker-compose.yml").exists()


def test_docker_compose_has_volume_for_data():
    content = (_REPO_ROOT / "docker/docker-compose.yml").read_text()
    assert "data" in content and "volumes" in content


def test_dockerignore_exists():
    assert (_REPO_ROOT / ".dockerignore").exists()


# ---------------------------------------------------------------------------
# 2. Environment-variable config overrides
# ---------------------------------------------------------------------------


def test_env_override_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUBERGE_DATA_DIR", str(tmp_path / "custom_data"))
    reset_config()
    config = load_config("/nonexistent/path.yaml")
    assert config.app.data_dir == str(tmp_path / "custom_data")


def test_env_override_host(monkeypatch):
    monkeypatch.setenv("AUBERGE_HOST", "127.0.0.1")
    reset_config()
    config = load_config("/nonexistent/path.yaml")
    assert config.app.host == "127.0.0.1"


def test_env_override_port(monkeypatch):
    monkeypatch.setenv("AUBERGE_PORT", "9000")
    reset_config()
    config = load_config("/nonexistent/path.yaml")
    assert config.app.port == 9000


def test_env_override_log_level(monkeypatch):
    monkeypatch.setenv("AUBERGE_LOG_LEVEL", "DEBUG")
    reset_config()
    config = load_config("/nonexistent/path.yaml")
    assert config.app.log_level == "DEBUG"


def test_env_override_user_name(monkeypatch):
    monkeypatch.setenv("AUBERGE_USER_NAME", "Alice")
    reset_config()
    config = load_config("/nonexistent/path.yaml")
    assert config.user.name == "Alice"


def test_env_override_takes_precedence_over_yaml(tmp_path, monkeypatch):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("app:\n  host: yaml-host\n")
    monkeypatch.setenv("AUBERGE_HOST", "env-host")
    reset_config()
    config = load_config(str(yaml_path))
    assert config.app.host == "env-host"


def test_no_env_override_uses_yaml(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("app:\n  host: yaml-host\n")
    reset_config()
    config = load_config(str(yaml_path))
    assert config.app.host == "yaml-host"


def test_scheduler_config_defaults():
    reset_config()
    config = load_config("/nonexistent/path.yaml")
    assert config.scheduler.enabled is False
    assert config.scheduler.interval_seconds == 86400
    assert config.scheduler.cleanup_older_than_days == 30


def test_scheduler_config_from_yaml(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "scheduler:\n  enabled: true\n  interval_seconds: 3600\n  cleanup_older_than_days: 7\n"
    )
    reset_config()
    config = load_config(str(yaml_path))
    assert config.scheduler.enabled is True
    assert config.scheduler.interval_seconds == 3600
    assert config.scheduler.cleanup_older_than_days == 7


# ---------------------------------------------------------------------------
# 3. Media cleanup
# ---------------------------------------------------------------------------


def _make_old_image(base: Path, session: str, filename: str, age_seconds: int) -> Path:
    img_dir = base / "images" / session
    img_dir.mkdir(parents=True, exist_ok=True)
    img = img_dir / filename
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    old_time = time.time() - age_seconds
    os.utime(img, (old_time, old_time))
    return img


def test_cleanup_deletes_old_images(tmp_path):
    img = _make_old_image(tmp_path, "tok1", "old.png", 40 * 86400)  # 40 days old
    deleted = cleanup_images(tmp_path, older_than_days=30)
    assert deleted == 1
    assert not img.exists()


def test_cleanup_keeps_recent_images(tmp_path):
    img = _make_old_image(tmp_path, "tok1", "new.png", 1 * 86400)  # 1 day old
    deleted = cleanup_images(tmp_path, older_than_days=30)
    assert deleted == 0
    assert img.exists()


def test_cleanup_no_images_dir(tmp_path):
    assert cleanup_images(tmp_path, older_than_days=30) == 0


def test_cleanup_api_endpoint(tmp_path):
    from aubergeRP.config import get_config
    from aubergeRP.main import create_app

    reset_config()
    get_config().app.data_dir = str(tmp_path)
    _make_old_image(tmp_path, "tok1", "old.png", 40 * 86400)
    _make_old_image(tmp_path, "tok1", "new.png", 1 * 86400)

    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/api/images/cleanup", json={"older_than_days": 30})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1


# ---------------------------------------------------------------------------
# 4. SQLite database storage
# ---------------------------------------------------------------------------


def _make_char_svc(tmp_path: Path) -> CharacterService:
    return CharacterService(data_dir=tmp_path)


def _make_conv_svc(tmp_path: Path, char_svc: CharacterService) -> ConversationService:
    return ConversationService(data_dir=tmp_path, character_service=char_svc)


def test_db_file_created(tmp_path):
    _make_char_svc(tmp_path)
    assert (tmp_path / "auberge.db").exists()


def test_character_stored_in_db(tmp_path):
    svc = _make_char_svc(tmp_path)
    card = svc.create_character(CharacterData(name="Elara", description="A ranger."))
    # Reload via a fresh service (same data dir)
    svc2 = _make_char_svc(tmp_path)
    found = svc2.get_character(card.id)
    assert found.data.name == "Elara"


def test_conversation_stored_in_db(tmp_path):
    char_svc = _make_char_svc(tmp_path)
    char = char_svc.create_character(CharacterData(name="Bob", description="A guard."))
    conv_svc = _make_conv_svc(tmp_path, char_svc)
    conv = conv_svc.create_conversation(char.id)
    # Reload
    loaded = conv_svc.get_conversation(conv.id)
    assert loaded.id == conv.id
    assert loaded.character_name == "Bob"


def test_message_stored_in_db(tmp_path):
    char_svc = _make_char_svc(tmp_path)
    char = char_svc.create_character(CharacterData(name="Bob", description="A guard."))
    conv_svc = _make_conv_svc(tmp_path, char_svc)
    conv = conv_svc.create_conversation(char.id)
    conv_svc.append_message(conv.id, "user", "Hello!")
    loaded = conv_svc.get_conversation(conv.id)
    user_msgs = [m for m in loaded.messages if m.role == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "Hello!"


# ---------------------------------------------------------------------------
# 5. Migration system
# ---------------------------------------------------------------------------


def test_migration_table_created(tmp_path):
    from sqlmodel import Session, select

    from aubergeRP.database import get_engine
    from aubergeRP.db_models import SchemaMigration

    init_db(tmp_path)
    with Session(get_engine(tmp_path)) as session:
        rows = session.exec(select(SchemaMigration)).all()
    assert len(rows) >= 1
    versions = [r.version for r in rows]
    assert 1 in versions


def test_migration_idempotent(tmp_path):
    """Running init_db twice does not duplicate migrations."""
    from sqlmodel import Session, select

    from aubergeRP.database import get_engine
    from aubergeRP.db_models import SchemaMigration

    init_db(tmp_path)
    init_db(tmp_path)  # second call
    with Session(get_engine(tmp_path)) as session:
        rows = session.exec(select(SchemaMigration)).all()
    # version 1 should appear only once
    v1 = [r for r in rows if r.version == 1]
    assert len(v1) == 1


def test_json_migration_imports_characters(tmp_path):
    """Existing JSON character files are imported into SQLite on first startup."""
    # Write a legacy JSON character file
    chars_dir = tmp_path / "characters"
    chars_dir.mkdir()
    from datetime import datetime

    now = datetime.now(UTC).isoformat()
    char_id = "legacy-char-001"
    (chars_dir / f"{char_id}.json").write_text(json.dumps({
        "id": char_id,
        "has_avatar": False,
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {"name": "Legacy", "description": "Old character.", "extensions": {}},
        "created_at": now,
        "updated_at": now,
    }))

    # Init DB — should pick up legacy JSON
    init_db(tmp_path)
    svc = _make_char_svc(tmp_path)
    found = svc.get_character(char_id)
    assert found.data.name == "Legacy"


def test_custom_migration_loaded_from_data_dir(tmp_path):
    """A custom migration script in {data_dir}/custom_migrations/ is applied."""
    custom_dir = tmp_path / "custom_migrations"
    custom_dir.mkdir()
    migration_file = custom_dir / "m002_test.py"
    migration_file.write_text(
        "DESCRIPTION = 'Test custom migration'\n"
        "def migrate(session):\n"
        "    pass  # No-op custom migration\n"
    )

    init_db(tmp_path)

    from sqlmodel import Session, select

    from aubergeRP.database import get_engine
    from aubergeRP.db_models import SchemaMigration

    with Session(get_engine(tmp_path)) as session:
        rows = session.exec(select(SchemaMigration)).all()
    versions = [r.version for r in rows]
    assert 2 in versions


def test_migration_012_adds_media_generation_columns(tmp_path):
    """The image generation trace columns exist and survive a re-run."""
    from sqlalchemy import text
    from sqlmodel import Session

    from aubergeRP.database import get_engine

    init_db(tmp_path)
    init_db(tmp_path)  # idempotent: ALTER TABLE must not run twice

    with Session(get_engine(tmp_path)) as session:
        cols = {
            row[1]
            for row in session.execute(text("PRAGMA table_info(media_library)")).all()
        }

    assert {
        "raw_prompt",
        "llm_input_prompt",
        "llm_output_prompt",
        "prompt_prefix",
        "negative_prompt",
        "connector_name",
    } <= cols


def test_migration_012_preserves_existing_media_rows(tmp_path):
    """A row created before m012 keeps its prompt and gets empty trace columns."""
    import uuid
    from datetime import datetime

    from sqlalchemy import create_engine, text
    from sqlmodel import Session

    from aubergeRP.migrations import m012_media_generation_details

    # Build the pre-m012 media_library schema so the ALTER TABLE is exercised.
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with Session(engine) as session:
        session.execute(
            text(
                """
                CREATE TABLE media_library (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '',
                    owner TEXT NOT NULL DEFAULT '',
                    media_type TEXT NOT NULL DEFAULT 'image',
                    media_url TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    generated_via_connector BOOLEAN NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
        )
        session.execute(
            text(
                "INSERT INTO media_library (id, conversation_id, message_id, owner, "
                "media_type, media_url, prompt, generated_via_connector, created_at) "
                "VALUES (:id, 'c1', 'm1', '', 'image', '/api/images/s/a.png', "
                "'legacy prompt', 1, :ts)"
            ),
            {"id": str(uuid.uuid4()), "ts": datetime.now(UTC).isoformat()},
        )
        session.commit()

    with Session(engine) as session:
        m012_media_generation_details.migrate(session)
        m012_media_generation_details.migrate(session)  # idempotent
        session.commit()
        row = session.execute(
            text("SELECT prompt, raw_prompt, negative_prompt FROM media_library")
        ).one()

    assert row[0] == "legacy prompt"
    assert row[1] == ""
    assert row[2] == ""
