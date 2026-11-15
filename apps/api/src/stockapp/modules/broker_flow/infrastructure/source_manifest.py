"""Reviewed capability registry, not a grant of supplier usage rights.

The official products remain disabled as recorded in Phase 2H-1. There is no
URL construction, authentication, CAPTCHA interaction or network request here.
An offline import contract cannot override this registry or grant display rights.
"""
from dataclasses import dataclass
from types import MappingProxyType


MANIFEST_VERSION = "broker-offline-manifest-v1"


@dataclass(frozen=True, kw_only=True)
class BrokerSourceManifest:
    key: str
    name: str
    audit_reference: str
    download_enabled: bool = False
    contract_verified: bool = False
    public_display_verified: bool = False
    derived_display_verified: bool = False
    disabled_reason: str = "project_source_usage_not_verified"


MANIFESTS = MappingProxyType({
    "twse_licensed": BrokerSourceManifest(key="twse_licensed", name="TWSE licensed broker reports",
                                         audit_reference="phase-2h-data-sources:H-B2"),
    "tpex_licensed": BrokerSourceManifest(key="tpex_licensed", name="TPEx licensed broker reports",
                                         audit_reference="phase-2h-data-sources:H-B4"),
})
