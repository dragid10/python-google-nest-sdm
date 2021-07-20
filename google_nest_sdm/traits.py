"""Base library for all traits."""

import aiohttp
from typing import Dict, Any

from .auth import AbstractAuth
from .registry import Registry

DEVICE_TRAITS = "traits"

TRAIT_MAP = Registry()


class Command:
    """Base class for executing commands."""

    def __init__(self, device_id: str, auth: AbstractAuth):
        """Initialize Command."""
        self._device_id = device_id
        self._auth = auth

    async def execute(self, data: Dict[str, Any]) -> aiohttp.ClientResponse:
        """Run the command."""
        return await self._auth.post(f"{self._device_id}:executeCommand", json=data)

    async def fetch_image(self, url: str, basic_auth=None) -> bytes:
        """Fetch an image at the specified url."""
        headers = None
        if basic_auth:
            headers = {"Authorization": f"Basic {basic_auth}"}
        resp = await self._auth.get(url, headers=headers)
        return await resp.read()


def _TraitsDict(
    traits: Dict[str, Any], trait_map: Dict[str, Any], cmd: Command
) -> Dict[str, Any]:
    d = {}
    for (trait, trait_data) in traits.items():
        if trait not in trait_map:
            continue
        cls = trait_map[trait]
        d[trait] = cls(trait_data, cmd)
    return d


def BuildTraits(
    traits: Dict[str, Any], cmd: Command, device_type=None
) -> Dict[str, Any]:
    """Build a trait map out of a response dict."""
    # There is a bug where doorbells do not return the DoorbellChime trait.  Simulate
    # that it was returned
    if device_type and device_type == "sdm.devices.types.DOORBELL":
        traits = traits.copy()
        traits["sdm.devices.traits.DoorbellChime"] = {}
    return _TraitsDict(traits, TRAIT_MAP, cmd)
