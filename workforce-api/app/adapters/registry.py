"""Versioned registry for all seven RunGrow product integrations."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .generic_product_adapter import GENERIC_PRODUCT_IDS, GenericProductAdapter
from .photo_studio_growth_os import PhotoStudioGrowthOSAdapter
from ..commercial_intelligence import PRODUCT_PLAYBOOKS


@dataclass(frozen=True)
class AdapterRegistration:
    product_id: str
    api_version: str
    adapter: Any
    commercial_stage: str
    readiness: str


class ProductAdapterRegistry:
    def __init__(self):
        self._items: dict[str, AdapterRegistration] = {}

    def register(self, registration: AdapterRegistration):
        if registration.product_id in self._items:
            raise ValueError(f"Adapter already registered: {registration.product_id}")
        if registration.api_version != "v1":
            raise ValueError("RunGrow product adapters must implement v1")
        self._items[registration.product_id] = registration

    def get(self, product_id: str):
        try:
            return self._items[product_id].adapter
        except KeyError as exc:
            raise LookupError(f"No adapter registered for product: {product_id}") from exc

    def registration(self, product_id: str) -> AdapterRegistration:
        try:
            return self._items[product_id]
        except KeyError as exc:
            raise LookupError(f"No adapter registered for product: {product_id}") from exc

    def readiness(self) -> list[dict[str, str]]:
        return [{
            "product_id": item.product_id,
            "api_version": item.api_version,
            "commercial_stage": item.commercial_stage,
            "readiness": item.readiness,
            "adapter_type": type(item.adapter).__name__,
        } for item in self._items.values()]

    @property
    def product_ids(self) -> frozenset[str]:
        return frozenset(self._items)


def _secret_for(product_id: str) -> str:
    env_key = "RUNGROW_ADAPTER_SECRET_" + product_id.upper().replace("-", "_")
    return os.getenv(env_key, os.getenv("RUNGROW_ADAPTER_WEBHOOK_SECRET", ""))


def build_product_adapter_registry() -> ProductAdapterRegistry:
    registry = ProductAdapterRegistry()
    flagship_secret = os.getenv("STUDIO_GROWTH_OS_SERVICE_SECRET", "")
    flagship = PhotoStudioGrowthOSAdapter(service_secret=flagship_secret or None)
    registry.register(AdapterRegistration(
        product_id="photo-studio-growth-os", api_version="v1", adapter=flagship,
        commercial_stage=PRODUCT_PLAYBOOKS["photo-studio-growth-os"]["stage"],
        readiness="READY" if flagship_secret else "REGISTERED_SECRET_REQUIRED"))
    for product_id in GENERIC_PRODUCT_IDS:
        secret = _secret_for(product_id)
        stage = PRODUCT_PLAYBOOKS[product_id]["stage"]
        adapter = GenericProductAdapter(product_id, service_secret=secret, commercial_stage=stage)
        registry.register(AdapterRegistration(
            product_id=product_id, api_version="v1", adapter=adapter,
            commercial_stage=stage,
            readiness=("DISCOVERY_ONLY" if stage == "DISCOVERY_ONLY" else
                       "READY" if secret else "REGISTERED_SECRET_REQUIRED")))
    return registry
