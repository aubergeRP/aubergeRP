from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class AubergerpExtensions(BaseModel):
    image_prompt_prefix: str = ""
    negative_prompt: str = ""


class ScheduleDefinition(BaseModel):
    """A single schedule entry stored inside a character card extension.

    This is the *definition* of a schedule (what to do and when).
    Runtime execution state is stored separately in :class:`ScheduleInstanceRow`.
    """

    id: str = Field(..., min_length=1, max_length=100)
    enabled: bool = True
    type: Literal["daily_at", "daily_window", "after_delay", "after_inactivity"]
    # Used by daily_at: local time "HH:MM"
    time: str | None = None
    # Used by daily_window: local time range "HH:MM"
    start: str | None = None
    end: str | None = None
    # Used by after_delay / after_inactivity
    delay_minutes: int | None = None
    inactivity_minutes: int | None = None
    # Optional local-time floor for event-based triggers ("after 09:00")
    not_before_time: str | None = None
    # Optional per-schedule cooldown override
    minimum_cooldown_minutes: int | None = None
    # One-off schedules disable themselves after first successful send/skip
    one_shot: bool = False
    # Natural-language instruction passed to the LLM at trigger time
    instruction: str = Field(..., min_length=1)


class ProactiveConfig(BaseModel):
    enabled: bool = True
    decision_mode: Literal["always_send", "contextual"] = "contextual"
    minimum_cooldown_minutes: int = 180
    maximum_active_schedules_per_conversation: int = 20
    maximum_scheduling_horizon_minutes: int = 60 * 24 * 30


class CharacterData(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    personality: str = ""
    first_mes: str = ""
    mes_example: str = ""
    scenario: str = ""
    system_prompt: str = ""
    post_history_instructions: str = ""
    creator: str = ""
    creator_notes: str = ""
    character_version: str = ""
    tags: list[str] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str]) -> list[str]:
        for tag in v:
            if len(tag) > 50:
                raise ValueError(f"Tag '{tag}' exceeds 50 characters")
        return v


class CharacterCard(BaseModel):
    id: str
    has_avatar: bool = False
    created_at: datetime
    updated_at: datetime
    spec: str = "chara_card_v2"
    spec_version: str = "2.0"
    data: CharacterData


class CharacterSummary(BaseModel):
    id: str
    name: str
    description: str
    avatar_url: str
    has_avatar: bool
    tags: list[str]
    created_at: datetime
    updated_at: datetime
