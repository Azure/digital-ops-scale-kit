"""Boundaries for declarative inputs that construct exactly one ordinary Site."""

import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from siteops.arm_resources import ArmResourceObservation
from siteops.guided_inputs import (
    load_contract,
    load_direct_site,
    write_yaml_exclusive,
)
from siteops.models import Site

VERSION = "siteops.inputs/v1"
GENERIC_MANIFEST = (
    Path(__file__).parent / "fixtures" / "browse-workspace"
    / "manifests" / "storage" / "manifest.yaml"
)


def _field(name, site_path, **extra):
    return {
        "name": name,
        "type": "string",
        "description": f"Enter {name}",
        "sitePath": site_path,
        **extra,
    }


def _resource_role(name="cluster", **overrides):
    return {
        "name": name,
        "type": "azureResourceId",
        "description": "Existing Azure resource ID.",
        "required": False,
        "resource": {
            "type": "Microsoft.Kubernetes/connectedClusters",
            "apiVersion": "2024-07-15-preview",
        },
        "derive": {
            "subscription": "subscription",
            "resourceGroup": "resourceGroup",
            "location": "location",
            "name": "clusterName",
        },
        **overrides,
    }


def _manual_resource_fields():
    return [
        _field("subscription", "subscription"),
        _field("resourceGroup", "resourceGroup"),
        _field("location", "location"),
        _field("clusterName", "parameters.clusterName"),
    ]


def _manifest_and_contract(tmp_path, *, fields=None, defaults=None):
    target = tmp_path / "manifests" / "storage"
    target.mkdir(parents=True)
    manifest = target / "manifest.yaml"
    manifest.write_bytes(GENERIC_MANIFEST.read_bytes())
    contract = target / "inputs.yaml"
    document = {
        "apiVersion": VERSION,
        "kind": "SiteInputContract",
        "siteDefaults": defaults if defaults is not None else {
            "labels": {"environment": "example"},
            "parameters": {"sku": "standard"},
        },
        "inputs": fields if fields is not None else [
            _field("subscription", "subscription"),
            _field("location", "location"),
            _field("resourceGroup", "resourceGroup", default="rg-example"),
            _field("usePrivate", "properties.options.private", type="boolean", default=False),
            _field(
                "privateName",
                "properties.options.privateName",
                required=True,
                when={"input": "usePrivate", "equals": True},
            ),
        ],
    }
    contract.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return manifest, contract


def _values(tmp_path, values):
    path = tmp_path / "answers.yaml"
    path.write_text(
        yaml.safe_dump(
            {"apiVersion": VERSION, "kind": "SiteInputValues", "values": values},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_contract_path_is_pure_and_depends_on_manifest_layout(tmp_path):
    from siteops.guided_inputs import contract_path

    root = tmp_path / "manifests"
    assert contract_path(root / "foo.yaml") == root / "foo.inputs.yaml"
    assert contract_path(root / "foo.yml") == root / "foo.inputs.yaml"
    assert contract_path(root / "foo" / "manifest.yaml") == root / "foo" / "inputs.yaml"
    assert contract_path(root / "foo" / "manifest.yml") == root / "foo" / "inputs.yaml"


def test_flat_manifest_has_own_contract_and_cannot_read_siblings_shared_sidecar(tmp_path):
    manifest, nested = _manifest_and_contract(tmp_path)
    directory = manifest.parent.parent
    flat = directory / "foo.yaml"
    sibling = directory / "bar.yaml"
    flat.write_bytes(GENERIC_MANIFEST.read_bytes())
    sibling.write_bytes(GENERIC_MANIFEST.read_bytes())
    (directory / "foo.inputs.yaml").write_bytes(nested.read_bytes())
    (directory / "inputs.yaml").write_bytes(nested.read_bytes())

    assert load_contract(flat).inputs[0].name == "subscription"
    assert load_contract(sibling) is None
    assert load_contract(manifest).inputs[0].name == "subscription"
    other_nested = directory / "bar" / "manifest.yml"
    other_nested.parent.mkdir()
    other_nested.write_bytes(GENERIC_MANIFEST.read_bytes())
    (other_nested.parent / "inputs.yaml").write_bytes(nested.read_bytes())
    assert load_contract(other_nested).inputs[0].name == "subscription"


def test_generic_fixture_builds_an_ordinary_site_from_typed_answers(tmp_path):
    manifest, _ = _manifest_and_contract(tmp_path)
    contract = load_contract(manifest)
    assert contract is not None
    assert isinstance(contract.inputs, tuple)
    assert contract.fields == contract.inputs
    with pytest.raises(FrozenInstanceError):
        contract.fields = ()
    assert len(contract.inputs) == 5
    assert contract.example() == {
        "apiVersion": VERSION,
        "kind": "SiteInputValues",
        "values": {"subscription": None, "location": None},
    }
    assert contract.describe()["inputs"][3]["default"] is False
    assert contract.describe()["inputs"][4]["status"] == "conditional"

    answers = _values(
        tmp_path,
        {"subscription": "sub-file", "location": "eastus", "usePrivate": True},
    )
    with pytest.raises(ValueError, match="privateName"):
        contract.resolve(values_file=answers)

    site = contract.resolve(values_file=answers, inline=["subscription=sub-inline", "privateName=key"])
    assert isinstance(site, Site)
    assert site == Site.from_data(
        {
            "labels": {"environment": "example"},
            "parameters": {"sku": "standard"},
            "subscription": "sub-inline",
            "location": "eastus",
            "resourceGroup": "rg-example",
            "properties": {"options": {"private": True, "privateName": "key"}},
        },
        source="guided inputs",
        default_name="guided-site",
    )
    assert site.name == "guided-site"
    assert contract.resolve(inline=[
        "subscription=sub-inline", "location=eastus", "usePrivate=true", "privateName=key",
    ]) == site


def test_pure_binding_and_site_construction_match_existing_resolution(tmp_path):
    manifest, _ = _manifest_and_contract(tmp_path)
    contract = load_contract(manifest)
    inline = ["subscription=sub", "location=eastus"]

    bound = contract.bind(inline=inline)

    assert bound.active_values["subscription"] == "sub"
    assert bound.active_values["usePrivate"] is False
    assert contract.build_site(bound) == contract.resolve(inline=inline)


def test_optional_cluster_reference_preserves_manual_answer_route(tmp_path):
    fields = [*_manual_resource_fields(), _resource_role()]
    manifest, _ = _manifest_and_contract(tmp_path, fields=fields)
    contract = load_contract(manifest)

    assert contract.describe()["inputs"][0]["derivableFrom"] == ["cluster"]
    assert contract.example()["values"] == {
        "subscription": None,
        "resourceGroup": None,
        "location": None,
        "clusterName": None,
        "cluster": None,
    }
    manual = dict(contract.example()["values"])
    manual.update(
        subscription="00000000-0000-0000-0000-000000000001",
        resourceGroup="rg-first", location="eastus", clusterName="arc-first",
    )
    manual_file = _values(tmp_path, manual)
    assert not contract.bind(values_file=manual_file).resources
    site = contract.resolve(inline=[
        "subscription=00000000-0000-0000-0000-000000000001",
        "resourceGroup=rg-first",
        "location=eastus",
        "clusterName=arc-first",
    ])
    assert site.subscription == "00000000-0000-0000-0000-000000000001"
    assert site.parameters["clusterName"] == "arc-first"
    assert contract.resolve(values_file=manual_file) == site


def test_resource_answer_requires_explicit_read_even_with_complete_manual_values(tmp_path):
    manifest, _ = _manifest_and_contract(
        tmp_path, fields=[*_manual_resource_fields(), _resource_role()],
    )
    reference = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-first/providers/Microsoft.Kubernetes/"
        "connectedClusters/arc-first"
    )
    with pytest.raises(ValueError, match="read-resources"):
        load_contract(manifest).resolve(inline=[
            "subscription=00000000-0000-0000-0000-000000000001",
            "resourceGroup=rg-first",
            "location=eastus",
            "clusterName=arc-first",
            f"cluster={reference}",
        ])


def test_resource_id_completes_null_manual_answers_after_observation(tmp_path):
    manifest, _ = _manifest_and_contract(
        tmp_path, fields=[*_manual_resource_fields(), _resource_role()],
    )
    contract = load_contract(manifest)
    reference = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-first/providers/Microsoft.Kubernetes/"
        "connectedClusters/arc-first"
    )
    answers = _values(tmp_path, {
        "subscription": None,
        "resourceGroup": None,
        "location": None,
        "clusterName": None,
        "cluster": reference,
    })

    bound = contract.bind(values_file=answers)

    assert [resource.field.name for resource in bound.resources] == ["cluster"]
    assert bound.active_values["subscription"] == "00000000-0000-0000-0000-000000000001"
    assert bound.active_values["resourceGroup"] == "rg-first"
    assert "location" not in bound.active_values
    observed = ArmResourceObservation(
        resource_id=reference,
        resource_type="Microsoft.Kubernetes/connectedClusters",
        location="eastus",
        name="arc-first",
        facts={},
    )
    site = contract.build_site(bound, {"cluster": observed})
    assert site.subscription == "00000000-0000-0000-0000-000000000001"
    assert site.resource_group == "rg-first"
    assert site.location == "eastus"
    assert site.parameters["clusterName"] == "arc-first"


def test_resource_id_conflicts_with_manual_subscription_before_provider_read(tmp_path):
    manifest, _ = _manifest_and_contract(
        tmp_path, fields=[*_manual_resource_fields(), _resource_role()],
    )
    reference = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-first/providers/Microsoft.Kubernetes/"
        "connectedClusters/arc-first"
    )
    with pytest.raises(ValueError, match="subscription.*conflicts"):
        load_contract(manifest).bind(inline=[
            "subscription=00000000-0000-0000-0000-000000000002",
            f"cluster={reference}",
        ])


def test_enabled_resource_requirement_needs_cluster_read_before_site_construction(tmp_path):
    enabled = _field(
        "enableSecretSync", "properties.deployOptions.enableSecretSync",
        type="boolean", default=False,
    )
    required = [
        {
            "fact": "connectedClusters.workloadIdentityEnabled",
            "when": {"input": "enableSecretSync", "equals": True},
            "description": "Cluster workload identity is enabled.",
        },
        {
            "fact": "connectedClusters.oidcIssuerAvailable",
            "when": {"input": "enableSecretSync", "equals": True},
            "description": "Cluster has an OIDC issuer.",
        },
    ]
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[*_manual_resource_fields(), enabled, _resource_role(requires=required)],
    )
    contract = load_contract(manifest)
    manual = [
        "subscription=00000000-0000-0000-0000-000000000001",
        "resourceGroup=rg-first", "location=eastus", "clusterName=arc-first",
    ]
    assert contract.resolve(inline=manual).properties["deployOptions"]["enableSecretSync"] is False
    with pytest.raises(ValueError, match="requirement-unverified"):
        contract.bind(inline=[*manual, "enableSecretSync=true"])


def test_inactive_resource_does_not_enforce_its_unconditional_requirement(tmp_path):
    enabled = _field(
        "enabled", "properties.enabled", type="boolean", default=False,
    )
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            *_manual_resource_fields(),
            enabled,
            _resource_role(
                when={"input": "enabled", "equals": True},
                sitePath="properties.cluster",
                derive={},
                requires=[{
                    "fact": "connectedClusters.oidcIssuerAvailable",
                    "description": "Existing cluster has an OIDC issuer.",
                }],
            ),
        ],
    )
    contract = load_contract(manifest)
    manual = [
        "subscription=00000000-0000-0000-0000-000000000001",
        "resourceGroup=rg-example", "location=eastus", "clusterName=arc-example",
    ]
    assert contract.resolve(inline=manual).properties["enabled"] is False
    with pytest.raises(ValueError, match="requirement-unverified"):
        contract.bind(inline=[*manual, "enabled=true"])
    cluster_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-example/providers/Microsoft.Kubernetes/connectedClusters/arc-example"
    )
    bound = contract.bind(inline=[*manual, "enabled=true", f"cluster={cluster_id}"])
    assert len(bound.resources) == 1
    assert bound.resources[0].ref.resource_id == cluster_id


def test_two_named_resources_bind_independently_without_forcing_same_group(tmp_path):
    cluster_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-cluster/providers/Microsoft.Kubernetes/"
        "connectedClusters/arc-cluster"
    )
    vault_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-vault/providers/Microsoft.KeyVault/vaults/vault-one"
    )
    fields = [
        *_manual_resource_fields(),
        _field(
            "enableSecretSync", "properties.deployOptions.enableSecretSync",
            type="boolean", default=False,
        ),
        _resource_role(),
        {
            "name": "existingVault", "type": "azureResourceId",
            "description": "Existing Key Vault.", "required": False,
            "when": {"input": "enableSecretSync", "equals": True},
            "sitePath": "parameters.existingKeyVaultResourceId",
            "resource": {"type": "Microsoft.KeyVault/vaults",
                         "apiVersion": "2023-07-01", "subscription": "site"},
        },
    ]
    manifest, _ = _manifest_and_contract(tmp_path, fields=fields)
    contract = load_contract(manifest)
    bound = contract.bind(inline=[
        "enableSecretSync=true", f"cluster={cluster_id}", f"existingVault={vault_id}",
    ])
    assert [resource.field.name for resource in bound.resources] == [
        "cluster", "existingVault",
    ]
    site = contract.build_site(bound, {
        "cluster": ArmResourceObservation(
            cluster_id, "Microsoft.Kubernetes/connectedClusters",
            "eastus", "arc-cluster", {},
        ),
        "existingVault": ArmResourceObservation(
            vault_id, "Microsoft.KeyVault/vaults", "westus", "vault-one", {},
        ),
    })
    assert site.resource_group == "rg-cluster"
    assert site.location == "eastus"
    assert site.parameters["existingKeyVaultResourceId"] == vault_id

    conflicting = vault_id.replace(
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )
    with pytest.raises(ValueError, match="subscription-mismatch"):
        contract.bind(inline=[
            "enableSecretSync=true", f"cluster={cluster_id}", f"existingVault={conflicting}",
        ])
    with pytest.raises(ValueError, match="inactive"):
        contract.bind(inline=[f"cluster={cluster_id}", f"existingVault={vault_id}"])


def test_site_subscription_constraint_uses_mapped_field_independent_of_role_order(tmp_path):
    sub_id = "00000000-0000-0000-0000-000000000001"
    other_id = "00000000-0000-0000-0000-000000000002"
    cluster_id = (
        f"/subscriptions/{sub_id}/resourceGroups/rg-cluster/"
        "providers/Microsoft.Kubernetes/connectedClusters/arc-cluster"
    )
    vault_id = (
        f"/subscriptions/{sub_id}/resourceGroups/rg-vault/"
        "providers/Microsoft.KeyVault/vaults/vault-one"
    )
    fields = [
        _field("targetSubscription", "subscription"),
        _field("subscription", "parameters.auditSubscription", required=False),
        _field("resourceGroup", "resourceGroup"),
        _field("location", "location"),
        _field("clusterName", "parameters.clusterName"),
        _field(
            "enableSecretSync", "properties.deployOptions.enableSecretSync",
            type="boolean", default=False,
        ),
        {
            "name": "existingVault", "type": "azureResourceId",
            "description": "Existing Key Vault.", "required": False,
            "when": {"input": "enableSecretSync", "equals": True},
            "sitePath": "parameters.existingKeyVaultResourceId",
            "resource": {"type": "Microsoft.KeyVault/vaults",
                         "apiVersion": "2023-07-01", "subscription": "site"},
        },
        _resource_role(derive={
            "subscription": "targetSubscription", "resourceGroup": "resourceGroup",
            "location": "location", "name": "clusterName",
        }),
    ]
    manifest, _ = _manifest_and_contract(tmp_path, fields=fields)
    contract = load_contract(manifest)
    bound = contract.bind(inline=[
        "enableSecretSync=true", f"existingVault={vault_id}",
        f"cluster={cluster_id}", f"subscription={other_id}",
    ])
    assert bound.active_values["targetSubscription"] == sub_id
    assert [resource.field.name for resource in bound.resources] == [
        "existingVault", "cluster",
    ]

    vault_other_sub = vault_id.replace(sub_id, other_id)
    with pytest.raises(ValueError, match="subscription-mismatch"):
        contract.bind(inline=[
            "enableSecretSync=true", f"existingVault={vault_other_sub}",
            f"cluster={cluster_id}", f"subscription={other_id}",
        ])


@pytest.mark.parametrize(
    ("role_change", "error"),
    [
        ({"resource": {"type": "Microsoft.Kubernetes/connectedClusters",
                       "apiVersion": "2024-07-15-preview", "provider": "arbitrary"}},
         "unknown"),
        ({"resource": {"type": "Microsoft.Kubernetes/connectedClusters",
                       "apiVersion": "latest"}}, "apiVersion"),
        ({"resource": {"type": "Microsoft.Kubernetes/connectedClusters",
                       "apiVersion": "2024-19-91"}}, "apiVersion"),
        ({"resource": {"type": "Microsoft.Kubernetes/connectedClusters",
                       "apiVersion": "2024-07-15-preview", "subscription": "another"}},
         "subscription"),
        ({"derive": {"id": "unregistered"}}, "derive"),
        ({"derive": {"subscription": "subscription"},
          "resource": {"type": "Microsoft.KeyVault/vaults",
                       "apiVersion": "2023-07-01", "subscription": "site"}},
         "subscription"),
        ({"default": "/subscriptions/example"}, "default"),
        ({"requires": [{"fact": "properties.oidcIssuerProfile.issuerUrl",
                        "description": "Do not expose issuer."}]}, "fact"),
        ({"requires": [{"fact": "connectedClusters.oidcIssuerAvailable",
                        "description": "Issuer is configured."}],
          "resource": {"type": "Microsoft.KeyVault/vaults",
                       "apiVersion": "2023-07-01"}}, "fact"),
    ],
)
def test_resource_declarations_reject_open_or_inconsistent_metadata(
    tmp_path, role_change, error,
):
    role = _resource_role(**role_change)
    manifest, _ = _manifest_and_contract(
        tmp_path, fields=[*_manual_resource_fields(), role],
    )
    with pytest.raises(ValueError, match=error):
        load_contract(manifest)


def test_resource_derivation_rejects_competing_or_defaulted_writers(tmp_path):
    manual = _manual_resource_fields()
    manual[0]["default"] = "sub"
    manifest, _ = _manifest_and_contract(
        tmp_path, fields=[*manual, _resource_role()],
    )
    with pytest.raises(ValueError, match="derive"):
        load_contract(manifest)

    manual[0].pop("default")
    manifest.parent.joinpath("inputs.yaml").write_text(
        yaml.safe_dump({
            "apiVersion": VERSION, "kind": "SiteInputContract",
            "inputs": [*manual, _resource_role(), _resource_role("second")],
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="derive"):
        load_contract(manifest)


def test_example_supports_disabled_condition_and_requires_active_dependent(tmp_path):
    manifest, _ = _manifest_and_contract(tmp_path)
    contract = load_contract(manifest)
    example = contract.example()
    assert example["values"] == {"subscription": None, "location": None}
    assert contract.describe()["inputs"][4]["status"] == "conditional"
    assert contract.describe()["inputs"][4]["when"] == {
        "input": "usePrivate", "equals": True,
    }

    example["values"].update(
        subscription="sub", location="eastus", usePrivate=False,
    )
    disabled = tmp_path / "disabled.yaml"
    write_yaml_exclusive(disabled, example)
    assert contract.resolve(values_file=disabled).properties == {
        "options": {"private": False},
    }

    example["values"]["usePrivate"] = True
    enabled = tmp_path / "enabled.yaml"
    write_yaml_exclusive(enabled, example)
    with pytest.raises(ValueError, match=r"Missing required input 'privateName'"):
        contract.resolve(values_file=enabled)


def test_missing_optional_controller_is_not_treated_as_false(tmp_path):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field("enable", "properties.enable", type="boolean", required=False),
            _field(
                "choice",
                "properties.choice",
                when={"input": "enable", "equals": True},
            ),
        ],
    )
    with pytest.raises(ValueError, match="enable"):
        load_contract(manifest).resolve()
    assert load_contract(manifest).resolve(inline=["enable=false"]).properties == {
        "enable": False,
    }
    with pytest.raises(ValueError, match="inactive|choice"):
        load_contract(manifest).resolve(inline=["enable=false", "choice=unused"])


@pytest.mark.parametrize("site_path", ["parameters.token", "properties.token", "name"])
def test_sensitive_contract_is_rejected_before_answers_or_site_construction(
    tmp_path, site_path,
):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field("secret", site_path, sensitive=True),
        ],
    )
    with pytest.raises(ValueError, match="sensitive|protected") as rejected:
        load_contract(manifest)
    assert "protected-value" not in str(rejected.value)


@pytest.mark.parametrize(
    ("field", "error"),
    [
        (_field("bad-name", "subscription"), "name|identifier"),
        (_field("script", "properties.x[0]"), "sitePath"),
        (_field("script", "metadata.name"), "sitePath"),
        (_field("script", "labels.foo.bar"), "sitePath"),
        (_field("script", "properties.x.*"), "sitePath"),
        (_field("script", "subscription", type="integer"), "type"),
        (_field("script", "subscription", default="a", sensitive=True), "sensitive|default"),
        (_field("script", "subscription", required="yes"), "required"),
        (_field("script", "subscription", extra="unknown"), "extra|unknown"),
        (_field("script", "subscription", when={"input": "script", "equals": "x"}), "when"),
    ],
)
def test_invalid_field_declarations_fail_before_resolution(tmp_path, field, error):
    manifest, _ = _manifest_and_contract(tmp_path, fields=[field])
    with pytest.raises(ValueError, match=error):
        load_contract(manifest)


@pytest.mark.parametrize(
    "path",
    [
        "properties." + ".".join(["a"] * 8),
        "properties." + ".".join(["a" * 34] * 6 + ["a" * 36]),
        "properties." + "a" * 65,
    ],
    ids=["nine-segments", "257-bytes", "65-character-identifier"],
)
def test_site_path_size_and_depth_are_bounded(tmp_path, path):
    manifest, _ = _manifest_and_contract(tmp_path, fields=[_field("deep", path)])
    with pytest.raises(ValueError, match="sitePath"):
        load_contract(manifest)


def test_site_path_accepts_eight_segments_at_256_bytes_and_64_character_identifiers(
    tmp_path,
):
    boundary = "properties." + ".".join(["a" * 34] * 6 + ["a" * 35])
    assert len(boundary.split(".")) == 8
    assert len(boundary.encode("utf-8")) == 256
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field("deep", boundary, default="mapped"),
            _field("long", "parameters." + "b" * 64, default="accepted"),
        ],
    )
    contract = load_contract(manifest)
    site = contract.resolve()
    nested = site.properties
    for part in boundary.split(".")[1:]:
        nested = nested[part]
    assert nested == "mapped"
    assert site.parameters["b" * 64] == "accepted"


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ([], "inputs"),
        ([_field("x", "subscription"), _field("x", "location")], "duplicate|x"),
        ([_field("x", "properties.x"), _field("y", "properties.x.child")], "overlap|path"),
        ([_field("x", "labels.a"), _field("y", "labels.a")], "overlap|path"),
        ([_field("x", "subscription", when={"input": "other", "equals": "x"})], "when"),
        ([_field("x", "subscription", when={"input": "x", "equals": "x"})], "when"),
        (
            [_field("enabled", "properties.enabled", type="boolean"),
             _field("x", "location", when={"input": "enabled", "equals": "true"})],
            "equals",
        ),
    ],
)
def test_declaration_conflicts_are_rejected(tmp_path, fields, error):
    manifest, _ = _manifest_and_contract(tmp_path, fields=fields)
    with pytest.raises(ValueError, match=error):
        load_contract(manifest)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"apiVersion": "future"}, "apiVersion"),
        ({"kind": "Site"}, "kind"),
        ({"extra": True}, "extra|unknown"),
        ({"siteDefaults": {"inherits": "other.yaml"}}, "siteDefaults|inherits"),
        ({"siteDefaults": {"name": "other"}}, "siteDefaults|name"),
        ({"siteDefaults": {"properties": []}}, "properties|mapping"),
        ({"inputs": [_field("x", "subscription")] * 65}, "64|inputs"),
    ],
)
def test_closed_contract_shape(tmp_path, change, error):
    manifest, path = _manifest_and_contract(tmp_path)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.update(change)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        load_contract(manifest)


def test_defaults_and_writers_do_not_overwrite_non_mapping_paths(tmp_path):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        defaults={"properties": {"options": "not-a-mapping"}},
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field("enabled", "properties.options.enabled", type="boolean", default=True),
        ],
    )
    with pytest.raises(ValueError, match="options|mapping"):
        load_contract(manifest).resolve()


@pytest.mark.parametrize("root", ["properties", "parameters"])
def test_writer_cannot_replace_dict_valued_site_default_even_when_optional(tmp_path, root):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        defaults={root: {"config": {"existing": "keep"}}},
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field("config", f"{root}.config", required=False),
        ],
    )
    with pytest.raises(ValueError, match="siteDefaults|mapping|dict"):
        load_contract(manifest)


@pytest.mark.parametrize("root", ["properties", "parameters"])
def test_writer_can_override_scalar_valued_site_default(tmp_path, root):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        defaults={root: {"config": "previous"}},
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field("config", f"{root}.config", default="new"),
        ],
    )
    site = load_contract(manifest).resolve()
    assert getattr(site, root)["config"] == "new"


def test_site_defaults_field_default_file_and_inline_precedence(tmp_path):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        defaults={"properties": {"mode": "site-default"}},
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field("mode", "properties.mode", default="field-default"),
        ],
    )
    contract = load_contract(manifest)
    assert contract.resolve().properties["mode"] == "field-default"
    answers = _values(tmp_path, {"mode": "file-value"})
    assert contract.resolve(values_file=answers).properties["mode"] == "file-value"
    assert contract.resolve(values_file=answers, inline=["mode=inline-value"]).properties == {
        "mode": "inline-value",
    }
    assert contract.resolve().properties["mode"] == "field-default"


def test_conditional_input_cannot_be_a_controller(tmp_path):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field("enable", "properties.enable", type="boolean", default=False),
            _field(
                "stage",
                "properties.stage",
                type="boolean",
                default=True,
                when={"input": "enable", "equals": True},
            ),
            _field(
                "name",
                "properties.name",
                when={"input": "stage", "equals": True},
            ),
        ],
    )
    with pytest.raises(ValueError, match="conditional|controller"):
        load_contract(manifest)


def test_multiple_dependents_can_share_an_unconditional_controller(tmp_path):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field("enable", "properties.enable", type="boolean", default=False),
            _field("first", "properties.first", when={"input": "enable", "equals": True}),
            _field("second", "properties.second", when={"input": "enable", "equals": True}),
        ],
    )
    contract = load_contract(manifest)
    assert contract.resolve().properties == {"enable": False}
    with pytest.raises(ValueError, match="first"):
        contract.resolve(inline=["enable=true"])
    assert contract.resolve(inline=["enable=true", "first=a", "second=b"]).properties == {
        "enable": True, "first": "a", "second": "b",
    }


def test_missing_required_name_is_safe_guided_error(tmp_path):
    from siteops.guided_inputs import GuidedInputError

    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            _field("siteName", "name"),
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
        ],
    )
    with pytest.raises(GuidedInputError) as rejected:
        load_contract(manifest).resolve()
    assert str(rejected.value) == "Missing required input 'siteName'."
    assert isinstance(rejected.value, ValueError)


def test_artifact_failure_is_not_classified_as_safe_guided_error(tmp_path, monkeypatch):
    from siteops import guided_inputs
    from siteops.artifacts import ArtifactError

    manifest, _ = _manifest_and_contract(tmp_path)

    def fail_open(path):
        raise ArtifactError("A package artifact changed.")

    monkeypatch.setattr(guided_inputs, "open_regular_file", fail_open)
    with pytest.raises(ArtifactError, match="package artifact changed"):
        load_contract(manifest)


def test_os_file_failure_is_not_classified_as_safe_guided_error(tmp_path, monkeypatch):
    from siteops import guided_inputs

    manifest, _ = _manifest_and_contract(tmp_path)

    def fail_open(path):
        raise PermissionError("File access denied.")

    monkeypatch.setattr(guided_inputs, "open_regular_file", fail_open)
    with pytest.raises(PermissionError, match="File access denied"):
        load_contract(manifest)


def test_site_parser_failure_is_not_classified_as_safe_guided_error(tmp_path):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[_field("subscription", "subscription", default="sub")],
    )
    with pytest.raises(ValueError, match="location") as rejected:
        load_contract(manifest).resolve()
    assert type(rejected.value) is ValueError


def test_example_fails_on_first_missing_required_input_before_site_parsing(tmp_path, monkeypatch):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            _field("siteName", "name"),
            _field("subscription", "subscription"),
            _field("location", "location"),
        ],
    )
    contract = load_contract(manifest)
    answers = tmp_path / "example.yaml"
    write_yaml_exclusive(answers, contract.example())
    assert yaml.safe_load(answers.read_text(encoding="utf-8"))["values"] == {
        "siteName": None,
        "subscription": None,
        "location": None,
    }

    def unexpected_site(*args, **kwargs):
        pytest.fail("An incomplete example must fail before constructing a Site or plan.")

    monkeypatch.setattr(Site, "from_data", unexpected_site)
    with pytest.raises(ValueError, match=r"^Missing required input 'siteName'\.$"):
        contract.resolve(values_file=answers)


def test_inline_answers_complete_only_matching_required_example_placeholders(tmp_path):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            _field("siteName", "name"),
            _field("subscription", "subscription"),
            _field("location", "location"),
        ],
    )
    contract = load_contract(manifest)
    answers = tmp_path / "example.yaml"
    write_yaml_exclusive(answers, contract.example())

    with pytest.raises(ValueError, match=r"Missing required input 'siteName'"):
        contract.resolve(values_file=answers, inline=["subscription=sub", "location=eastus"])
    with pytest.raises(ValueError, match=r"Missing required input 'subscription'"):
        contract.resolve(values_file=answers, inline=["siteName=one"])

    site = contract.resolve(
        values_file=answers,
        inline=["siteName=one", "subscription=sub", "location=eastus"],
    )
    assert (site.name, site.subscription, site.location) == ("one", "sub", "eastus")


@pytest.mark.parametrize(
    ("name", "kind", "site_path", "file_value", "inline", "message"),
    [
        ("nickname", "string", "properties.nickname", None, "nickname=valid", "string"),
        ("siteName", "string", "name", False, "siteName=one", "string"),
        ("enabled", "boolean", "properties.enabled", "true", "enabled=true", "boolean"),
    ],
)
def test_inline_does_not_mask_invalid_file_values(
    tmp_path, name, kind, site_path, file_value, inline, message,
):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field(name, site_path, type=kind, required=name != "nickname"),
        ],
    )
    with pytest.raises(ValueError, match=f"Input '{name}' must be a {message}"):
        load_contract(manifest).resolve(
            values_file=_values(tmp_path, {name: file_value}),
            inline=[inline],
        )


def test_required_boolean_placeholder_accepts_only_typed_inline_override(tmp_path):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field("enabled", "properties.enabled", type="boolean"),
        ],
    )
    contract = load_contract(manifest)
    answers = tmp_path / "example.yaml"
    write_yaml_exclusive(answers, contract.example())
    assert contract.resolve(values_file=answers, inline=["enabled=false"]).properties == {
        "enabled": False,
    }
    with pytest.raises(ValueError, match="boolean"):
        contract.resolve(values_file=answers, inline=["enabled=False"])


@pytest.mark.parametrize(
    ("name", "kind", "value", "message"),
    [
        ("nickname", "string", None, "must be a string"),
        ("enabled", "boolean", None, "must be a boolean"),
        ("enabled", "boolean", "true", "must be a boolean"),
    ],
)
def test_optional_null_and_nonboolean_file_values_still_fail(
    tmp_path, name, kind, value, message,
):
    manifest, _ = _manifest_and_contract(
        tmp_path,
        fields=[
            _field("subscription", "subscription", default="sub"),
            _field("location", "location", default="eastus"),
            _field(name, f"properties.{name}", type=kind, required=False),
        ],
    )
    with pytest.raises(ValueError, match=message):
        load_contract(manifest).resolve(values_file=_values(tmp_path, {name: value}))


@pytest.mark.parametrize(
    ("inline", "error"),
    [
        (["unknown=x"], "unknown"),
        (["location=eastus", "location=westus"], "duplicate|location"),
        (["location"], "NAME=VALUE|format"),
        (["usePrivate=yes"], "boolean|true|false"),
        (["subscription=  "], "subscription|required"),
    ],
)
def test_inline_answers_are_typed_and_unique(tmp_path, inline, error):
    manifest, _ = _manifest_and_contract(tmp_path)
    with pytest.raises(ValueError, match=error):
        load_contract(manifest).resolve(inline=inline)


@pytest.mark.parametrize(
    ("contents", "error"),
    [
        ("apiVersion: siteops.inputs/v1\nkind: SiteInputValues\nvalues:\n  location: eastus\n"
         "  location: westus\n", "duplicate"),
        ("apiVersion: siteops.inputs/v1\nkind: SiteInputValues\nvalues:\n  5: x\n", "string|key"),
        ("apiVersion: siteops.inputs/v1\nkind: SiteInputValues\nvalues:\n  unknown: x\n", "unknown"),
        ("apiVersion: siteops.inputs/v1\nkind: SiteInputValues\nvalues:\n  usePrivate: 'true'\n",
         "boolean"),
        ("apiVersion: siteops.inputs/v1\nkind: SiteInputValues\nvalues: []\n", "mapping"),
        ("apiVersion: siteops.inputs/v1\nkind: SiteInputValues\nvalues: {}\nother: x\n",
         "unknown"),
        ("apiVersion: siteops.inputs/v2\nkind: SiteInputValues\nvalues: {}\n", "apiVersion"),
        ("apiVersion: siteops.inputs/v1\nkind: Site\nvalues: {}\n", "kind"),
    ],
)
def test_invalid_answer_files_are_rejected(tmp_path, contents, error):
    manifest, _ = _manifest_and_contract(tmp_path)
    path = tmp_path / "answers.yaml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        load_contract(manifest).resolve(values_file=path)


def test_contract_missing_is_optional_but_not_derived_from_advisory_inputs(tmp_path):
    manifest, path = _manifest_and_contract(tmp_path)
    path.unlink()
    assert load_contract(manifest) is None


def test_contract_and_answers_are_bounded_and_reject_links(tmp_path):
    manifest, contract_path = _manifest_and_contract(tmp_path)
    contract_path.write_bytes(b" " * (128 * 1024 + 1))
    with pytest.raises(ValueError, match="limit|size"):
        load_contract(manifest)
    contract_path.unlink()
    _manifest_and_contract(tmp_path / "new")
    manifest2 = tmp_path / "new" / "manifests" / "storage" / "manifest.yaml"
    values = tmp_path / "oversized.yaml"
    values.write_bytes(b" " * (128 * 1024 + 1))
    with pytest.raises(ValueError, match="limit|size"):
        load_contract(manifest2).resolve(values_file=values)


def test_contract_symlink_is_not_treated_as_absent_or_opened(tmp_path):
    manifest, contract_path = _manifest_and_contract(tmp_path)
    contract_path.unlink()
    target = tmp_path / "outside.yaml"
    target.write_text("kind: SiteInputContract\n", encoding="utf-8")
    try:
        contract_path.symlink_to(target)
    except OSError:
        pytest.skip("Symbolic links are unavailable on this host.")
    with pytest.raises(ValueError, match="link|regular"):
        load_contract(manifest)


def test_duplicate_contract_keys_are_rejected(tmp_path):
    manifest, path = _manifest_and_contract(tmp_path)
    path.write_text(
        "apiVersion: siteops.inputs/v1\nkind: SiteInputContract\n"
        "inputs:\n  - name: subscription\n    name: location\n"
        "    type: string\n    description: Test\n    sitePath: subscription\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_contract(manifest)


def test_package_binding_validates_before_presence_and_reads_verified_contract(tmp_path):
    manifest, contract_path = _manifest_and_contract(tmp_path)

    class Binding:
        def __init__(self):
            self.calls = []
            self.expected = contract_path.read_bytes()

        def validate(self):
            self.calls.append("validate")
            if contract_path.exists() and contract_path.read_bytes() != self.expected:
                raise ValueError("Package bytes changed.")

        def require_workspace_file(self, path):
            self.calls.append("require_workspace_file")
            if path != contract_path or path.is_symlink():
                raise ValueError("Package path is not verified.")
            return path, "manifests/storage/inputs.yaml"

    binding = Binding()
    assert load_contract(manifest, binding=binding) is not None
    assert binding.calls == [
        "validate", "require_workspace_file", "require_workspace_file",
    ]
    binding.calls.clear()
    contract_path.unlink()
    assert load_contract(manifest, binding=binding) is None
    assert binding.calls == ["validate"]
    contract_path.write_bytes(binding.expected + b"# tampered\n")
    with pytest.raises(ValueError, match="Package bytes changed"):
        load_contract(manifest, binding=binding)
    contract_path.write_bytes(binding.expected)
    outside = tmp_path / "other" / "manifest.yaml"
    outside.parent.mkdir()
    (outside.parent / "inputs.yaml").write_bytes(binding.expected)
    with pytest.raises(ValueError, match="Package path is not verified"):
        load_contract(outside, binding=binding)


@pytest.mark.parametrize("envelope", [False, True])
def test_direct_site_file_rejects_inheritance_without_loading_a_parent(tmp_path, envelope):
    parent = tmp_path / "base.yaml"
    parent.write_text("subscription: sub\nlocation: eastus\n", encoding="utf-8")
    child = tmp_path / "child.yaml"
    if envelope:
        child.write_text(
            "apiVersion: siteops/v1\nkind: Site\ninherits: base.yaml\n"
            "metadata:\n  name: child\nspec:\n  subscription: sub\n  location: eastus\n",
            encoding="utf-8",
        )
    else:
        child.write_text(
            "apiVersion: siteops/v1\nkind: Site\ninherits: base.yaml\n"
            "name: child\nsubscription: sub\nlocation: eastus\n",
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match="inherits"):
        load_direct_site(child)
    child.write_text(
        "apiVersion: siteops/v1\nkind: Site\nname: child\n"
        "subscription: sub\nlocation: eastus\n",
        encoding="utf-8",
    )
    assert load_direct_site(child) == Site.from_data(
        {"name": "child", "subscription": "sub", "location": "eastus"},
        source=child,
        default_name="child",
    )


def test_direct_site_file_checks_kind_and_structure(tmp_path):
    path = tmp_path / "site.yaml"
    path.write_text("kind: SiteTemplate\nsubscription: sub\nlocation: eastus\n", encoding="utf-8")
    with pytest.raises(ValueError, match="kind"):
        load_direct_site(path)
    path.write_text("kind: Site\napiVersion: [bad]\nsubscription: sub\nlocation: eastus\n")
    with pytest.raises(ValueError, match="apiVersion"):
        load_direct_site(path)
    path.write_text("kind: Site\nsubscription: sub\nlocation: eastus\nlabels: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="labels"):
        load_direct_site(path)
    path.write_text("kind: Site\nsubscription: sub\nlocation: eastus\n" + " " * (128 * 1024))
    with pytest.raises(ValueError, match="limit|size"):
        load_direct_site(path)


def test_exclusive_yaml_writer_does_not_overwrite_or_create_parents(tmp_path):
    destination = tmp_path / "site.yaml"
    data = {"apiVersion": "siteops/v1", "kind": "Site", "subscription": "sub"}
    write_yaml_exclusive(destination, data)
    assert yaml.safe_load(destination.read_text(encoding="utf-8")) == data
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_yaml_exclusive(destination, {"subscription": "different"})
    assert yaml.safe_load(destination.read_text(encoding="utf-8")) == data
    with pytest.raises(FileNotFoundError):
        write_yaml_exclusive(tmp_path / "missing" / "site.yaml", data)
    assert not (tmp_path / "missing").exists()
    with pytest.raises(ValueError, match="mapping"):
        write_yaml_exclusive(tmp_path / "invalid.yaml", [])
    assert not (tmp_path / "invalid.yaml").exists()
