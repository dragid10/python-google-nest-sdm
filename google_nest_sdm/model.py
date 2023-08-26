"""Base model for all nest trait based classes."""

from __future__ import annotations

import datetime
from typing import Any

try:
    from pydantic.v1 import BaseModel, root_validator
except ImportError:
    from pydantic import BaseModel, root_validator  # type: ignore

TRAITS = "traits"
SDM_PREFIX = "sdm."


class TraitModel(BaseModel):
    """Base model for API objects that are trait based.

    This is meant to be subclasses by the model definitions.
    """

    _EXCLUDE_FIELDS = set(
        {
            "_trait_event_ts",
        }
    )

    def __init__(self, **data: Any):
        """Initialize TraitModel."""
        super().__init__(**data)
        datetime.datetime.now(datetime.timezone.utc)
        self._trait_event_ts: dict[str, datetime.datetime] = {}

    @property
    def traits(self) -> dict[str, Any]:
        """Return a trait mixin on None."""
        return {
            field.alias: getattr(self, field.name)
            for field in self.__fields__.values()
            if getattr(self, field.name) is not None
            and field.alias.startswith(SDM_PREFIX)
        }

    @root_validator(pre=True)
    def _parse_traits(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Parse traits as primary members of this class."""
        if traits := values.get(TRAITS):
            values.update(traits)
        return values

    def update_traits(
        self, traits: dict[str, Any], update_timestamp: datetime.datetime
    ) -> None:
        """Helper to update traits from pubsub messages."""
        for field in self.__fields__.values():
            if field.alias in traits:
                if (
                    self._trait_event_ts
                    and (ts := self._trait_event_ts.get(field.alias))
                    and ts > update_timestamp
                ):
                    # Discard updates older than prior events
                    continue
                setattr(self, field.name, traits[field.alias])
                self._trait_event_ts[field.alias] = update_timestamp

    @property
    def raw_data(self) -> dict[str, Any]:
        """Return raw data for the object."""
        return self.dict(
            by_alias=True,
            exclude=self._EXCLUDE_FIELDS,
            exclude_unset=True,
            exclude_defaults=True,
        )

    class Config:
        extra = "allow"
