# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Provider-neutral, value-safe observations of explicitly selected ARM resources."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from siteops.planning import CapabilityProviderIdentity

_ERROR_TEXT = MappingProxyType(
    {
        "INVALID_ID": "The ARM resource ID is not a supported single-resource ID.",
        "INVALID_TYPE": "The ARM resource type is not supported by this contract.",
        "TYPE_MISMATCH": "The ARM resource ID does not have the expected resource type.",
        "INVALID_API_VERSION": "The ARM API version must be a pinned calendar date.",
        "INVALID_OBSERVATION": "The ARM resource response has invalid or mismatched data.",
        "UNSUPPORTED_FACT": "The requested ARM resource fact is not supported.",
        "UNSUPPORTED_PROVIDER": "The selected ARM resource reader is not available.",
        "TOOL_MISSING": "The selected Azure CLI could not be started.",
        "TIMEOUT": "The ARM resource read exceeded its deadline.",
        "RESPONSE_LIMIT": "The ARM resource read exceeded its output limit.",
        "NOT_FOUND": "The ARM resource was not found.",
        "FORBIDDEN": "The selected session cannot read the ARM resource.",
        "NOT_LOGGED_IN": "The selected Azure CLI session is not signed in.",
        "SUBSCRIPTION_MISSING": "The subscription is not available to the selected session.",
        "CANCELLED": "The ARM resource read was cancelled.",
        "FAILED": "The ARM resource read failed.",
    }
)
_GUID = r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"
_RESOURCE_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)+/[A-Za-z][A-Za-z0-9]*")
_RESOURCE_ID = re.compile(
    rf"/subscriptions/(?P<subscription>{_GUID})"
    r"/resourceGroups/(?P<resource_group>[A-Za-z0-9][A-Za-z0-9._()-]{0,89})"
    r"/providers/(?P<resource_type>"
    + _RESOURCE_TYPE.pattern
    + r")/(?P<name>[A-Za-z0-9][A-Za-z0-9._()-]{0,259})",
    re.IGNORECASE | re.ASCII,
)
_API_VERSION = re.compile(r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})(?:-preview)?")
_LOCATION = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[ -][A-Za-z0-9]+)*")
_CONNECTED_CLUSTER = "microsoft.kubernetes/connectedclusters"
_CLUSTER_WORKLOAD = "connectedClusters.workloadIdentityEnabled"
_CLUSTER_OIDC = "connectedClusters.oidcIssuerAvailable"
_CLUSTER_FACTS = frozenset(
    {
        _CLUSTER_WORKLOAD,
        _CLUSTER_OIDC,
    }
)


class ArmResourceError(ValueError):
    """A stable error category whose text cannot contain untrusted values."""

    def __init__(self, code: str):
        if code not in _ERROR_TEXT:
            raise ValueError("Unsupported ARM resource error code.")
        self.code = code
        super().__init__(_ERROR_TEXT[code])


def _validated_parts(value: str, expected_type: str, api_version: str) -> re.Match[str]:
    if not isinstance(api_version, str) or not (version := _API_VERSION.fullmatch(api_version)):
        raise ArmResourceError("INVALID_API_VERSION")
    try:
        date.fromisoformat(version.group("date"))
    except ValueError:
        raise ArmResourceError("INVALID_API_VERSION") from None
    if (
        not isinstance(expected_type, str)
        or len(expected_type) > 128
        or not _RESOURCE_TYPE.fullmatch(expected_type)
    ):
        raise ArmResourceError("INVALID_TYPE")
    if not isinstance(value, str) or len(value) > 512:
        raise ArmResourceError("INVALID_ID")
    match = _RESOURCE_ID.fullmatch(value)
    if match is None:
        raise ArmResourceError("INVALID_ID")
    if match.group("resource_type").casefold() != expected_type.casefold():
        raise ArmResourceError("TYPE_MISMATCH")
    return match


@dataclass(frozen=True)
class ArmResourceRef:
    """An explicitly selected top-level ARM resource and pinned API version."""

    resource_id: str = field(repr=False)
    subscription: str
    resource_group: str
    resource_type: str
    name: str
    api_version: str

    def __post_init__(self) -> None:
        match = _validated_parts(self.resource_id, self.resource_type, self.api_version)
        if any(
            not isinstance(value, str) or value.casefold() != match.group(key).casefold()
            for key, value in (
                ("subscription", self.subscription),
                ("resource_group", self.resource_group),
                ("name", self.name),
            )
        ):
            raise ArmResourceError("INVALID_ID")


@dataclass(frozen=True)
class ArmResourceObservation:
    """Only requested, closed boolean facts may leave the provider adapter."""

    resource_id: str = field(repr=False)
    resource_type: str
    location: str
    name: str
    facts: Mapping[str, bool]

    def __post_init__(self) -> None:
        if not isinstance(self.facts, Mapping):
            raise ArmResourceError("INVALID_OBSERVATION")
        facts = dict(self.facts)
        if any(
            name not in _CLUSTER_FACTS or type(value) is not bool for name, value in facts.items()
        ):
            raise ArmResourceError("INVALID_OBSERVATION")
        object.__setattr__(self, "facts", MappingProxyType(facts))


@runtime_checkable
class ArmResourceReader(Protocol):
    identity: CapabilityProviderIdentity

    def read(
        self, ref: ArmResourceRef, *, facts: frozenset[str] = frozenset()
    ) -> ArmResourceObservation: ...


def parse_arm_resource_id(value: str, *, expected_type: str, api_version: str) -> ArmResourceRef:
    """Parse only a single subscription-scoped resource with one type/name pair."""
    match = _validated_parts(value, expected_type, api_version)
    return ArmResourceRef(
        resource_id=value,
        subscription=match.group("subscription"),
        resource_group=match.group("resource_group"),
        resource_type=expected_type,
        name=match.group("name"),
        api_version=api_version,
    )


def _validate_requested_facts(ref: ArmResourceRef, facts: frozenset[str]) -> None:
    if (
        not isinstance(facts, frozenset)
        or not facts.issubset(_CLUSTER_FACTS)
        or (facts and ref.resource_type.casefold() != _CONNECTED_CLUSTER)
    ):
        raise ArmResourceError("UNSUPPORTED_FACT")


def normalize_arm_resource_facts(
    ref: ArmResourceRef, document: Mapping[str, object], *, facts: frozenset[str]
) -> dict[str, bool]:
    """Extract only requested boolean facts from a private, bounded ARM document."""
    if not isinstance(ref, ArmResourceRef):
        raise ArmResourceError("INVALID_ID")
    _validate_requested_facts(ref, facts)
    if not isinstance(document, Mapping):
        raise ArmResourceError("INVALID_OBSERVATION")
    if not facts:
        return {}
    properties = document.get("properties")
    if not isinstance(properties, Mapping):
        raise ArmResourceError("INVALID_OBSERVATION")
    observed: dict[str, bool] = {}
    if _CLUSTER_WORKLOAD in facts:
        security_profile = properties.get("securityProfile")
        if security_profile is None:
            observed[_CLUSTER_WORKLOAD] = False
        else:
            if not isinstance(security_profile, Mapping):
                raise ArmResourceError("INVALID_OBSERVATION")
            workload_profile = security_profile.get("workloadIdentity")
            if workload_profile is None:
                observed[_CLUSTER_WORKLOAD] = False
            else:
                if not isinstance(workload_profile, Mapping):
                    raise ArmResourceError("INVALID_OBSERVATION")
                enabled = workload_profile.get("enabled")
                if type(enabled) is not bool:
                    raise ArmResourceError("INVALID_OBSERVATION")
                observed[_CLUSTER_WORKLOAD] = enabled
    if _CLUSTER_OIDC in facts:
        oidc_profile = properties.get("oidcIssuerProfile")
        if oidc_profile is None:
            observed[_CLUSTER_OIDC] = False
        else:
            if not isinstance(oidc_profile, Mapping):
                raise ArmResourceError("INVALID_OBSERVATION")
            issuer = oidc_profile.get("issuerUrl")
            if issuer is not None and not isinstance(issuer, str):
                raise ArmResourceError("INVALID_OBSERVATION")
            observed[_CLUSTER_OIDC] = bool(issuer and issuer.strip())
    return observed


def validate_arm_observation(ref: ArmResourceRef, observation: ArmResourceObservation) -> None:
    """Reject a provider response that cannot be tied to the requested resource."""
    if not isinstance(ref, ArmResourceRef) or not isinstance(observation, ArmResourceObservation):
        raise ArmResourceError("INVALID_OBSERVATION")
    try:
        returned = parse_arm_resource_id(
            observation.resource_id,
            expected_type=ref.resource_type,
            api_version=ref.api_version,
        )
    except ArmResourceError:
        raise ArmResourceError("INVALID_OBSERVATION") from None
    if (
        returned.resource_id.casefold() != ref.resource_id.casefold()
        or not isinstance(observation.resource_type, str)
        or observation.resource_type.casefold() != ref.resource_type.casefold()
        or not isinstance(observation.name, str)
        or observation.name.casefold() != ref.name.casefold()
        or not isinstance(observation.location, str)
        or len(observation.location) > 128
        or not _LOCATION.fullmatch(observation.location)
    ):
        raise ArmResourceError("INVALID_OBSERVATION")
    _validate_requested_facts(ref, frozenset(observation.facts))


def _azure_cli_reader() -> ArmResourceReader:
    from siteops.arm_resources_azure_cli import AzureCliArmReader

    return AzureCliArmReader()


_READERS: dict[str, Callable[[], ArmResourceReader]] = {"azure-cli": _azure_cli_reader}


def new_arm_reader(selected: str = "azure-cli") -> ArmResourceReader:
    """Construct only the operator-selected adapter, with no provider fallback."""
    if not isinstance(selected, str) or selected not in _READERS:
        raise ArmResourceError("UNSUPPORTED_PROVIDER")
    return _READERS[selected]()
