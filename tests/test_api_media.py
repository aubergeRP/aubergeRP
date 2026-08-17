from __future__ import annotations

from fastapi.testclient import TestClient

from aubergeRP.config import get_config, reset_config
from aubergeRP.main import create_app
from aubergeRP.models.character import CharacterData
from aubergeRP.services.character_service import CharacterService
from aubergeRP.services.conversation_service import ConversationService
from aubergeRP.services.media_service import GeneratedMedia, MediaService


def _bootstrap(tmp_path):
    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    media_svc = MediaService(data_dir=tmp_path)

    char = char_svc.create_character(CharacterData(name="Mina", description="Mage"))
    conv = conv_svc.create_conversation(char.id, owner="session-1")
    msg = conv_svc.append_message(
        conv.id,
        "assistant",
        "Here is the image",
        images=["/api/images/session-1/example.png"],
    )

    images_dir = tmp_path / "images" / "session-1"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / "example.png").write_bytes(b"PNGDATA")

    media_svc.record_generated_media(
        conversation_id=conv.id,
        message_id=msg.id,
        media_items=[
            GeneratedMedia(
                media_url="/api/images/session-1/example.png",
                prompt="oil painting, a fantasy tavern at dusk",
                raw_prompt="tavern",
                llm_input_prompt="## Character\nName: Mina",
                llm_output_prompt="a fantasy tavern at dusk",
                prompt_prefix="oil painting,",
                negative_prompt="blurry, watermark",
                connector_name="ComfyUI",
            )
        ],
    )

    return conv_svc


def test_media_list_endpoint_returns_prompt(tmp_path):
    reset_config()
    get_config().app.data_dir = str(tmp_path)
    _bootstrap(tmp_path)

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/media/")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["page"] == 1
    items = payload["items"]
    assert len(items) == 1
    assert items[0]["media_type"] == "image"
    assert items[0]["prompt"] == "oil painting, a fantasy tavern at dusk"
    assert items[0]["media_url"] == "/api/images/session-1/example.png"
    # Every pipeline step is exposed so the admin page can display the chain.
    assert items[0]["raw_prompt"] == "tavern"
    assert items[0]["llm_input_prompt"].startswith("## Character")
    assert items[0]["llm_output_prompt"] == "a fantasy tavern at dusk"
    assert items[0]["prompt_prefix"] == "oil painting,"
    assert items[0]["negative_prompt"] == "blurry, watermark"
    assert items[0]["connector_name"] == "ComfyUI"


def test_media_list_endpoint_pagination(tmp_path):
    reset_config()
    get_config().app.data_dir = str(tmp_path)

    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    media_svc = MediaService(data_dir=tmp_path)

    char = char_svc.create_character(CharacterData(name="Test", description="Tester"))
    conv = conv_svc.create_conversation(char.id, owner="session-2")
    msg = conv_svc.append_message(conv.id, "assistant", "msg")

    images_dir = tmp_path / "images" / "session-2"
    images_dir.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        fname = f"img{i}.png"
        (images_dir / fname).write_bytes(b"PNGDATA")
        media_svc.record_generated_media(
            conversation_id=conv.id,
            message_id=msg.id,
            media_items=[GeneratedMedia(media_url=f"/api/images/session-2/{fname}", prompt=f"prompt {i}")],
        )

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/media/?page=1&per_page=3")
        assert resp.status_code == 200
        page1 = resp.json()
        assert page1["total"] == 5
        assert page1["page"] == 1
        assert page1["per_page"] == 3
        assert len(page1["items"]) == 3

        resp2 = client.get("/api/media/?page=2&per_page=3")
        assert resp2.status_code == 200
        page2 = resp2.json()
        assert page2["total"] == 5
        assert len(page2["items"]) == 2


def test_media_list_endpoint_filter_by_type(tmp_path):
    reset_config()
    get_config().app.data_dir = str(tmp_path)

    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    media_svc = MediaService(data_dir=tmp_path)

    char = char_svc.create_character(CharacterData(name="Test", description="Tester"))
    conv = conv_svc.create_conversation(char.id, owner="session-3")
    msg = conv_svc.append_message(conv.id, "assistant", "msg")

    images_dir = tmp_path / "images" / "session-3"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / "img.png").write_bytes(b"PNGDATA")
    media_svc.record_generated_media(
        conversation_id=conv.id,
        message_id=msg.id,
        media_items=[GeneratedMedia(media_url="/api/images/session-3/img.png", prompt="image prompt")],
    )
    media_svc.record_generated_media(
        conversation_id=conv.id,
        message_id=msg.id,
        media_items=[GeneratedMedia(media_url="/api/images/session-3/clip.mp4", prompt="video prompt")],
    )

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/media/?media_type=image")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["media_type"] == "image"


def test_media_delete_endpoint_removes_db_message_link_and_file(tmp_path):
    reset_config()
    get_config().app.data_dir = str(tmp_path)
    conv_svc = _bootstrap(tmp_path)

    app = create_app()
    with TestClient(app) as client:
        listed = client.get("/api/media/")
        media_id = listed.json()["items"][0]["id"]

        resp = client.delete(f"/api/media/{media_id}")
        assert resp.status_code == 204

        listed_after = client.get("/api/media/")
        assert listed_after.status_code == 200
        assert listed_after.json()["total"] == 0
        assert listed_after.json()["items"] == []

    conv = conv_svc.list_conversations()[0]
    full = conv_svc.get_conversation(conv.id)
    assistant_with_images = [m for m in full.messages if m.role == "assistant" and "Here is the image" in m.content]
    assert len(assistant_with_images) == 1
    assert assistant_with_images[0].images == []
    assert not (tmp_path / "images" / "session-1" / "example.png").exists()
