"""Runtime settings that are common to all supported instruments."""

from pydantic import BaseModel


class RuntimeSettings(BaseModel, frozen=True):
    """Settings whose defaults make session handling deterministic."""

    timezone: str = "Asia/Kolkata"
