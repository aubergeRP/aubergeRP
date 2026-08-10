"""Tests for all Pydantic models: instantiation, validation, JSON round-trip."""
import json
from datetime import UTC, datetime

import pytest

NOW = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# models/character.py
# ---------------------------------------------------------------------------

from aubergeRP.models.character import (
    AubergerpExtensions,
    CharacterCard,
    CharacterData,
    CharacterSummary,
)


class TestCharacterData:
    def _valid(self, **overrides):
        base = {"name": "Elara", "description": "An elven ranger."}
        base.update(overrides)
        return CharacterData(**base)

    def test_instantiation_minimal(self):
        cd = self._valid()
        assert cd.name == "Elara"
        assert cd.description == "An elven ranger."
        assert cd.tags == []
        assert cd.extensions == {}

    def test_instantiation_full(self):
        cd = self._valid(
            personality="Calm",
            first_mes="Hello",
            mes_example="<START>",
            scenario="A forest",
            system_prompt="Stay in character.",
            post_history_instructions="[end]",
            creator="Dev",
            creator_notes="Notes here",
            character_version="1.0",
            tags=["elf", "ranger"],
            extensions={"aubergerp": {"image_prompt_prefix": "elf woman"}},
        )
        assert cd.tags == ["elf", "ranger"]
        assert cd.extensions["aubergerp"]["image_prompt_prefix"] == "elf woman"

    def test_name_required(self):
        with pytest.raises(Exception):
            CharacterData(description="ok")

    def test_description_required(self):
        with pytest.raises(Exception):
            CharacterData(name="X")

    def test_name_empty_string_rejected(self):
        with pytest.raises(Exception):
            CharacterData(name="", description="ok")

    def test_description_empty_string_rejected(self):
        with pytest.raises(Exception):
            CharacterData(name="X", description="")

    def test_name_max_length(self):
        with pytest.raises(Exception):
            CharacterData(name="a" * 201, description="ok")

    def test_tag_max_length(self):
        with pytest.raises(Exception):
            CharacterData(name="X", description="ok", tags=["a" * 51])

    def test_tags_valid_max_length(self):
        cd = self._valid(tags=["a" * 50])
        assert len(cd.tags[0]) == 50

    def test_json_roundtrip(self):
        cd = self._valid(tags=["elf"], personality="Wise")
        data = json.loads(cd.model_dump_json())
        cd2 = CharacterData(**data)
        assert cd2.name == cd.name
        assert cd2.tags == cd.tags


class TestCharacterCard:
    def _data(self):
        return CharacterData(name="Elara", description="An elven ranger.")

    def test_instantiation(self):
        card = CharacterCard(id="uuid-1", created_at=NOW, updated_at=NOW, data=self._data())
        assert card.spec == "chara_card_v2"
        assert card.spec_version == "2.0"
        assert card.has_avatar is False

    def test_id_required(self):
        with pytest.raises(Exception):
            CharacterCard(created_at=NOW, updated_at=NOW, data=self._data())

    def test_json_roundtrip(self):
        card = CharacterCard(id="uuid-1", created_at=NOW, updated_at=NOW, data=self._data())
        raw = json.loads(card.model_dump_json())
        card2 = CharacterCard(**raw)
        assert card2.id == card.id
        assert card2.data.name == card.data.name


class TestCharacterSummary:
    def test_instantiation(self):
        s = CharacterSummary(
            id="u1",
            name="Elara",
            description="desc",
            avatar_url="/api/characters/u1/avatar",
            has_avatar=True,
            tags=["elf"],
            created_at=NOW,
            updated_at=NOW,
        )
        assert s.avatar_url == "/api/characters/u1/avatar"

    def test_json_roundtrip(self):
        s = CharacterSummary(
            id="u1", name="X", description="d", avatar_url="/x",
            has_avatar=False, tags=[], created_at=NOW, updated_at=NOW,
        )
        raw = json.loads(s.model_dump_json())
        s2 = CharacterSummary(**raw)
        assert s2.id == s.id


class TestAubergerpExtensions:
    def test_defaults(self):
        ext = AubergerpExtensions()
        assert ext.image_prompt_prefix == ""
        assert ext.negative_prompt == ""

    def test_set_values(self):
        ext = AubergerpExtensions(image_prompt_prefix="elf", negative_prompt="blurry")
        assert ext.image_prompt_prefix == "elf"


# ---------------------------------------------------------------------------
# models/conversation.py
# ---------------------------------------------------------------------------

from aubergeRP.models.conversation import (
    Conversation,
    ConversationCreate,
    ConversationSummary,
    Message,
)


class TestMessage:
    def test_valid_roles(self):
        for role in ("user", "assistant", "system"):
            m = Message(id="m1", role=role, content="hi", timestamp=NOW)
            assert m.role == role

    def test_invalid_role(self):
        with pytest.raises(Exception):
            Message(id="m1", role="moderator", content="hi", timestamp=NOW)

    def test_images_default_empty(self):
        m = Message(id="m1", role="user", content="hi", timestamp=NOW)
        assert m.images == []

    def test_json_roundtrip(self):
        m = Message(id="m1", role="assistant", content="Hello!", images=["/api/images/x"], timestamp=NOW)
        raw = json.loads(m.model_dump_json())
        m2 = Message(**raw)
        assert m2.images == ["/api/images/x"]
        assert m2.role == "assistant"

    def test_required_fields(self):
        with pytest.raises(Exception):
            Message(role="user", content="hi", timestamp=NOW)  # missing id


class TestConversation:
    def _msg(self):
        return Message(id="m1", role="assistant", content="Hello!", timestamp=NOW)

    def test_instantiation(self):
        c = Conversation(
            id="c1", character_id="ch1", character_name="Elara",
            title="Elara — 2025-01-15 10:30",
            messages=[self._msg()],
            created_at=NOW, updated_at=NOW,
        )
        assert len(c.messages) == 1
        assert c.title == "Elara — 2025-01-15 10:30"

    def test_messages_default_empty(self):
        c = Conversation(
            id="c1", character_id="ch1", character_name="Elara",
            title="t", created_at=NOW, updated_at=NOW,
        )
        assert c.messages == []

    def test_json_roundtrip(self):
        c = Conversation(
            id="c1", character_id="ch1", character_name="X",
            title="t", messages=[self._msg()], created_at=NOW, updated_at=NOW,
        )
        raw = json.loads(c.model_dump_json())
        c2 = Conversation(**raw)
        assert c2.id == c.id
        assert len(c2.messages) == 1


class TestConversationSummary:
    def test_instantiation(self):
        s = ConversationSummary(
            id="c1", character_id="ch1", character_name="Elara",
            title="t", message_count=5, created_at=NOW, updated_at=NOW,
        )
        assert s.message_count == 5

    def test_json_roundtrip(self):
        s = ConversationSummary(
            id="c1", character_id="ch1", character_name="X",
            title="t", message_count=0, created_at=NOW, updated_at=NOW,
        )
        raw = json.loads(s.model_dump_json())
        s2 = ConversationSummary(**raw)
        assert s2.message_count == 0


class TestConversationCreate:
    def test_requires_character_id(self):
        with pytest.raises(Exception):
            ConversationCreate()

    def test_valid(self):
        cc = ConversationCreate(character_id="uuid-123")
        assert cc.character_id == "uuid-123"


# ---------------------------------------------------------------------------
# models/chat.py
# ---------------------------------------------------------------------------

from aubergeRP.models.chat import (
    ChatMessageRequest,
    DoneEvent,
    ErrorEvent,
    ImageCompleteEvent,
    ImageFailedEvent,
    ImageStartEvent,
    TokenEvent,
)


class TestChatModels:
    def test_chat_message_request_valid(self):
        r = ChatMessageRequest(content="Hello!")
        assert r.content == "Hello!"

    def test_chat_message_request_empty_rejected(self):
        with pytest.raises(Exception):
            ChatMessageRequest(content="")

    def test_token_event(self):
        e = TokenEvent(content="tok")
        assert json.loads(e.model_dump_json())["content"] == "tok"

    def test_image_start_event(self):
        e = ImageStartEvent(generation_id="g1", prompt="an elf")
        assert e.generation_id == "g1"

    def test_image_complete_event(self):
        e = ImageCompleteEvent(generation_id="g1", image_url="/api/images/x/y")
        assert e.image_url == "/api/images/x/y"

    def test_image_failed_event(self):
        e = ImageFailedEvent(generation_id="g1", detail="timeout")
        assert e.detail == "timeout"

    def test_done_event(self):
        e = DoneEvent(message_id="m1", full_content="Hello!", images=["/img/1"])
        raw = json.loads(e.model_dump_json())
        assert raw["images"] == ["/img/1"]

    def test_done_event_images_default_empty(self):
        e = DoneEvent(message_id="m1", full_content="")
        assert e.images == []

    def test_error_event(self):
        e = ErrorEvent(detail="LLM unreachable")
        assert e.detail == "LLM unreachable"


# ---------------------------------------------------------------------------
# models/connector.py
# ---------------------------------------------------------------------------

from aubergeRP.models.connector import (
    ConnectorActivateResult,
    ConnectorCreate,
    ConnectorInstance,
    ConnectorResponse,
    ConnectorTestResult,
    OpenAIImageConfig,
    OpenAITextConfig,
)


class TestOpenAITextConfig:
    def test_defaults(self):
        c = OpenAITextConfig()
        assert c.base_url == "http://localhost:11434/v1"
        assert c.model == "llama3"
        assert c.max_tokens == 1024
        assert c.temperature == 0.8
        assert c.timeout == 120

    def test_custom_values(self):
        c = OpenAITextConfig(base_url="http://custom/v1", api_key="sk-abc", model="mistral")
        assert c.api_key == "sk-abc"

    def test_json_roundtrip(self):
        c = OpenAITextConfig(model="phi3")
        raw = json.loads(c.model_dump_json())
        c2 = OpenAITextConfig(**raw)
        assert c2.model == "phi3"

    def test_extra_body_empty_string_coerced_to_dict(self):
        # Regression: admin form sends "" for empty object fields; must not raise.
        c = OpenAITextConfig(extra_body="")
        assert c.extra_body == {}

    def test_extra_body_none_coerced_to_dict(self):
        c = OpenAITextConfig(extra_body=None)
        assert c.extra_body == {}

    def test_extra_body_dict_preserved(self):
        c = OpenAITextConfig(extra_body={"provider": {"allow_fallbacks": False}})
        assert c.extra_body == {"provider": {"allow_fallbacks": False}}


class TestOpenAIImageConfig:
    def test_defaults(self):
        c = OpenAIImageConfig()
        assert c.model == "google/gemini-2.0-flash-exp:free"
        assert c.size == "1024x1024"

    def test_json_roundtrip(self):
        c = OpenAIImageConfig(api_key="sk-or-xxx")
        raw = json.loads(c.model_dump_json())
        c2 = OpenAIImageConfig(**raw)
        assert c2.api_key == "sk-or-xxx"


class TestConnectorInstance:
    def test_instantiation(self):
        ci = ConnectorInstance(
            id="c1", name="My Ollama", type="text", backend="openai_api",
            config={"base_url": "http://localhost:11434/v1", "model": "llama3"},
            created_at=NOW, updated_at=NOW,
        )
        assert ci.type == "text"
        assert ci.config["model"] == "llama3"

    def test_invalid_type(self):
        with pytest.raises(Exception):
            ConnectorInstance(
                id="c1", name="X", type="fax", backend="openai_api",
                config={}, created_at=NOW, updated_at=NOW,
            )

    def test_invalid_backend(self):
        with pytest.raises(Exception):
            ConnectorInstance(
                id="c1", name="X", type="text", backend="unknown_backend",
                config={}, created_at=NOW, updated_at=NOW,
            )

    def test_json_roundtrip(self):
        ci = ConnectorInstance(
            id="c1", name="Y", type="image", backend="openai_api",
            config={"api_key": "sk-x"}, created_at=NOW, updated_at=NOW,
        )
        raw = json.loads(ci.model_dump_json())
        ci2 = ConnectorInstance(**raw)
        assert ci2.id == ci.id
        assert ci2.config["api_key"] == "sk-x"


class TestConnectorResponse:
    def test_instantiation(self):
        r = ConnectorResponse(
            id="c1", name="My Ollama", type="text", backend="openai_api",
            is_active=True,
            config={"base_url": "http://localhost:11434/v1", "model": "llama3", "api_key_set": False},
            created_at=NOW, updated_at=NOW,
        )
        assert r.is_active is True
        assert r.config["api_key_set"] is False

    def test_json_roundtrip(self):
        r = ConnectorResponse(
            id="c1", name="X", type="image", backend="openai_api",
            is_active=False, config={"api_key_set": True}, created_at=NOW, updated_at=NOW,
        )
        raw = json.loads(r.model_dump_json())
        r2 = ConnectorResponse(**raw)
        assert r2.is_active is False


class TestConnectorCreate:
    def test_valid(self):
        cc = ConnectorCreate(
            name="Ollama", type="text", backend="openai_api",
            config={"base_url": "http://localhost:11434/v1", "model": "llama3"},
        )
        assert cc.name == "Ollama"

    def test_name_required(self):
        with pytest.raises(Exception):
            ConnectorCreate(type="text", backend="openai_api", config={})

    def test_name_empty_rejected(self):
        with pytest.raises(Exception):
            ConnectorCreate(name="", type="text", backend="openai_api", config={})


class TestConnectorTestResult:
    def test_connected_true(self):
        r = ConnectorTestResult(connected=True, details={"models_available": ["llama3"]})
        assert r.connected is True

    def test_json_roundtrip(self):
        r = ConnectorTestResult(connected=False, details={"error": "refused"})
        raw = json.loads(r.model_dump_json())
        r2 = ConnectorTestResult(**raw)
        assert r2.connected is False


class TestConnectorActivateResult:
    def test_instantiation(self):
        r = ConnectorActivateResult(id="c1", type="text", is_active=True)
        assert r.is_active is True

    def test_json_roundtrip(self):
        r = ConnectorActivateResult(id="c1", type="image", is_active=True)
        raw = json.loads(r.model_dump_json())
        r2 = ConnectorActivateResult(**raw)
        assert r2.type == "image"


# ---------------------------------------------------------------------------
# models/config.py
# ---------------------------------------------------------------------------

from aubergeRP.models.config import (
    ActiveConnectorsResponse,
    AppConfigResponse,
    ConfigResponse,
    ConfigUpdate,
    UserConfigResponse,
)


class TestConfigModels:
    def test_app_config_response(self):
        a = AppConfigResponse(host="0.0.0.0", port=8123, log_level="INFO")
        assert a.log_level == "INFO"

    def test_app_config_invalid_log_level(self):
        with pytest.raises(Exception):
            AppConfigResponse(host="0.0.0.0", port=8123, log_level="VERBOSE")

    def test_config_response_full(self):
        cr = ConfigResponse(
            app=AppConfigResponse(host="0.0.0.0", port=8123, log_level="DEBUG"),
            user=UserConfigResponse(name="Alice"),
            active_connectors=ActiveConnectorsResponse(text="uuid-1", image="uuid-2"),
        )
        assert cr.user.name == "Alice"
        assert cr.active_connectors.text == "uuid-1"

    def test_active_connectors_defaults(self):
        ac = ActiveConnectorsResponse()
        assert ac.text == ""
        assert ac.image == ""

    def test_config_update_all_optional(self):
        cu = ConfigUpdate()
        assert cu.app is None
        assert cu.user is None

    def test_json_roundtrip(self):
        cr = ConfigResponse(
            app=AppConfigResponse(host="127.0.0.1", port=9000, log_level="WARNING"),
            user=UserConfigResponse(name="Bob"),
            active_connectors=ActiveConnectorsResponse(text="", image=""),
        )
        raw = json.loads(cr.model_dump_json())
        cr2 = ConfigResponse(**raw)
        assert cr2.app.port == 9000
        assert cr2.user.name == "Bob"
