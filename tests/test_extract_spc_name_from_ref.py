"""Unit tests for `_extract_spc_name_from_ref`.

The AIO operator's projection of `defaultSecretProviderClassRef` onto
the cluster CR varies across operator versions. The helper accepts
multiple shapes (direct `name`, ARM `resourceId`, bare `id`) and the
contract is unit-tested here so a regression in shape handling does not
require a live E2E to surface.
"""

from __future__ import annotations

from tests.integration.test_secretsync_manifest import _extract_spc_name_from_ref


def test_returns_none_for_non_dict():
    assert _extract_spc_name_from_ref(None) is None
    assert _extract_spc_name_from_ref("string") is None
    assert _extract_spc_name_from_ref([]) is None


def test_returns_none_for_empty_dict():
    assert _extract_spc_name_from_ref({}) is None


def test_extracts_direct_name_field():
    assert _extract_spc_name_from_ref({"name": "spc-ops-abc"}) == "spc-ops-abc"


def test_extracts_from_resource_id():
    ref = {
        "resourceId": (
            "/subscriptions/00000000-0000-0000-0000-000000000000"
            "/resourceGroups/rg-test"
            "/providers/Microsoft.SecretSyncController"
            "/azureKeyVaultSecretProviderClasses/spc-ops-jas32kxw4x4o2"
        )
    }
    assert _extract_spc_name_from_ref(ref) == "spc-ops-jas32kxw4x4o2"


def test_extracts_from_bare_id_field():
    ref = {
        "id": (
            "/subscriptions/00000000-0000-0000-0000-000000000000"
            "/resourceGroups/rg/providers/X/Y/spc-name"
        )
    }
    assert _extract_spc_name_from_ref(ref) == "spc-name"


def test_name_field_wins_over_resource_id():
    """When both are present, the direct name is authoritative."""
    ref = {
        "name": "from-name",
        "resourceId": "/subscriptions/.../from-resource-id",
    }
    assert _extract_spc_name_from_ref(ref) == "from-name"


def test_trailing_slash_in_resource_id_is_tolerated():
    ref = {"resourceId": "/a/b/c/spc-trailing/"}
    assert _extract_spc_name_from_ref(ref) == "spc-trailing"


def test_returns_none_for_unparseable_resource_id():
    assert _extract_spc_name_from_ref({"resourceId": "no-slashes"}) is None
    assert _extract_spc_name_from_ref({"resourceId": None}) is None
    assert _extract_spc_name_from_ref({"resourceId": 42}) is None
