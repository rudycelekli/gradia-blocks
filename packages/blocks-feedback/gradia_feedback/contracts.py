from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Principal(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(default="configured-actor", min_length=1, max_length=100)
    role: Literal["submitter", "operator"] = "submitter"


class Design(StrictModel):
    accent: str = Field(default="#714b35", pattern=r"^#[0-9a-fA-F]{6}$")
    radius: int = Field(default=12, ge=0, le=32)
    density: Literal["comfortable", "compact"] = "comfortable"
    placement: Literal["bottom-right", "bottom-left", "inline"] = "bottom-right"


class Content(StrictModel):
    title: str = Field(default="Share feedback", min_length=1, max_length=100)
    launcher_label: str = Field(default="Feedback", min_length=1, max_length=60)
    message_label: str = Field(default="What should we improve?", min_length=1, max_length=160)
    success_message: str = Field(default="Your feedback was saved.", min_length=1, max_length=160)


class Features(StrictModel):
    enabled: bool = True
    audio: bool = False
    triage: bool = True


class RuntimeConfig(StrictModel):
    categories: tuple[str, ...] = ("general", "bug", "idea")
    max_message_chars: int = Field(default=10000, ge=1, le=10000)
    max_audio_bytes: int = Field(default=262144, ge=1, le=262144)

    @field_validator("categories")
    @classmethod
    def valid_categories(cls, value):
        if not value or len(value) > 20 or len(set(value)) != len(value):
            raise ValueError("Provide 1-20 unique categories")
        if any(not item or len(item) > 60 or not item.replace("-", "").replace("_", "").isalnum() for item in value):
            raise ValueError("Categories must be short identifiers")
        return value


class BlockSettings(StrictModel):
    design: Design = Field(default_factory=Design)
    content: Content = Field(default_factory=Content)
    features: Features = Field(default_factory=Features)
    config: RuntimeConfig = Field(default_factory=RuntimeConfig)


class Context(StrictModel):
    page_url: str | None = Field(default=None, max_length=2000)
    app_id: str | None = Field(default=None, max_length=100)

    @field_validator("page_url")
    @classmethod
    def safe_url(cls, value):
        if value is not None and not (value.startswith("https://") or value.startswith("http://") or (value.startswith("/") and not value.startswith("//"))):
            raise ValueError("Use an HTTP(S) URL or relative path")
        return value


class Audio(StrictModel):
    mime_type: Literal["audio/webm", "audio/ogg", "audio/wav"]
    data_base64: str = Field(min_length=1, max_length=349528)


class Submission(StrictModel):
    message: str = Field(min_length=1, max_length=10000)
    category: str = Field(default="general", min_length=1, max_length=60)
    context: Context = Field(default_factory=Context)
    audio: Audio | None = None

    @field_validator("message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Message cannot be blank")
        return value


class Triage(StrictModel):
    expected_revision: int = Field(ge=1)
    status: Literal["new", "in_review", "resolved", "dismissed"] | None = None
    category: str | None = Field(default=None, min_length=1, max_length=60)
    assignee: str | None = Field(default=None, max_length=100)


class AudioMetadata(StrictModel):
    mime_type: Literal["audio/webm", "audio/ogg", "audio/wav"]
    size_bytes: int
    sha256: str


class FeedbackRecord(StrictModel):
    id: str
    revision: int
    status: Literal["new", "in_review", "resolved", "dismissed"]
    category: str
    message: str
    assignee: str | None
    context: Context
    audio: AudioMetadata | None
    created_at: str
    updated_at: str


class Acknowledgment(StrictModel):
    status: Literal["acknowledged"]
    feedback: FeedbackRecord
    replayed: bool


class TriageAcknowledgment(StrictModel):
    status: Literal["acknowledged"]
    feedback: FeedbackRecord


class FeedbackPage(StrictModel):
    items: list[FeedbackRecord]
    next_cursor: str | None
