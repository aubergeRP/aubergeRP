from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class MediaItem(BaseModel):
    id: str
    conversation_id: str
    message_id: str
    owner: str = ""
    media_type: str
    media_url: str
    prompt: str = ""
    raw_prompt: str = ""
    llm_input_prompt: str = ""
    llm_output_prompt: str = ""
    prompt_prefix: str = ""
    negative_prompt: str = ""
    connector_name: str = ""
    generated_via_connector: bool = True
    created_at: datetime


class MediaPage(BaseModel):
    items: list[MediaItem]
    total: int
    page: int
    per_page: int
