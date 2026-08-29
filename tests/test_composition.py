"""Fast contracts for generic parameter composition."""

from pathlib import Path

import pytest
import yaml

from siteops.composition import (
    CompositionError,
    LoadedParameterSource,
    compose_sources,
    load_contract,
)

CONTRACT = """
apiVersion: siteops/v1
kind: ParameterComposition
name: test-catalog
collections:
  devices:
    path: devices
    identity:
      name: name
    members:
      inboundEndpoints:
        path: properties.endpoints.inbound
        shape: map
  assets:
    path: assets
    identity:
      name: name
  dataflowEndpoints:
    path: dataflowEndpoints
    identity:
      name: name
    seeds:
      - name: default
  dataflowProfiles:
    path: dataflowProfiles
    identity:
      name: name
    seeds:
      - name: default
  dataflows:
    path: dataflows
    identity:
      profile:
        path: profileRef
        default: default
      name: name
references:
  - id: asset-device-endpoint
    source:
      collection: assets
      select: properties.deviceRef
      bind:
        device: deviceName
        endpoint: endpointName
    target:
      collection: devices
      match:
        name: device
      member:
        name: inboundEndpoints
        match:
          key: endpoint
  - id: dataflow-profile
    source:
      collection: dataflows
      bind:
        profile:
          path: profileRef
          default: default
    target:
      collection: dataflowProfiles
      match:
        name: profile
  - id: dataflow-source-asset
    source:
      collection: dataflows
      select: properties.operations[*]
      bind:
        asset:
          path: sourceSettings.assetRef
          optional: true
    unverified: Asset reference value domain is not confirmed.
"""


@pytest.fixture
def contract(tmp_path):
    path = tmp_path / "contract.yaml"
    path.write_text(CONTRACT)
    return load_contract(path)


def _source(
    tmp_path: Path,
    name: str,
    data: dict,
    *collections: str,
) -> LoadedParameterSource:
    return LoadedParameterSource(
        path=tmp_path / name,
        data=data,
        collections=collections,
    )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("references", "references must be a list"),
        ("members", "members must be a mapping"),
    ],
)
def test_contract_rejects_wrong_optional_collection_shapes(
    tmp_path,
    case,
    message,
):
    path = tmp_path / "contract.yaml"
    data = yaml.safe_load(CONTRACT)
    if case == "references":
        data["references"] = "invalid"
    else:
        data["collections"]["devices"]["members"] = None
    path.write_text(yaml.safe_dump(data, sort_keys=False))

    with pytest.raises(CompositionError, match=message):
        load_contract(path)


def _device(name="plant-opc", endpoints=("opc",)):
    return {
        "name": name,
        "properties": {
            "endpoints": {
                "inbound": {
                    endpoint: {"endpointType": "Microsoft.OpcUa"}
                    for endpoint in endpoints
                }
            }
        },
    }


def _asset(device="plant-opc", endpoint="opc"):
    return {
        "name": "oven",
        "properties": {
            "deviceRef": {
                "deviceName": device,
                "endpointName": endpoint,
            }
        },
    }


def test_distinct_entries_compose_in_source_order(contract, tmp_path):
    result = compose_sources(
        contract,
        [
            _source(
                tmp_path,
                "shared.yaml",
                {"devices": [_device("first")]},
                "devices",
            ),
            _source(
                tmp_path,
                "site.yaml",
                {"devices": [_device("second")]},
                "devices",
            ),
        ],
    )

    assert [entry["name"] for entry in result.parameters["devices"]] == [
        "first",
        "second",
    ]


def test_duplicate_writer_is_rejected(contract, tmp_path):
    with pytest.raises(CompositionError, match="both write devices"):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "shared.yaml",
                    {"devices": [_device()]},
                    "devices",
                ),
                _source(
                    tmp_path,
                    "site.yaml",
                    {"devices": [_device()]},
                    "devices",
                ),
            ],
        )


def test_identity_whitespace_is_rejected_instead_of_normalized(
    contract,
    tmp_path,
):
    with pytest.raises(CompositionError, match="leading or trailing"):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "devices.yaml",
                    {"devices": [_device("plant-opc ")]},
                    "devices",
                )
            ],
        )


def test_composite_identity_allows_the_same_name_under_different_parents(
    contract,
    tmp_path,
):
    result = compose_sources(
        contract,
        [
            _source(
                tmp_path,
                "flows.yaml",
                {
                    "dataflowProfiles": [
                        {"name": "production", "properties": {}}
                    ],
                    "dataflows": [
                        {
                            "name": "publish",
                            "properties": {"operations": []},
                        },
                        {
                            "profileRef": "production",
                            "name": "publish",
                            "properties": {"operations": []},
                        },
                    ],
                },
                "dataflowProfiles",
                "dataflows",
            )
        ],
    )

    assert [
        entry.identity for entry in result.entries["dataflows"]
    ] == [("default", "publish"), ("production", "publish")]


def test_source_must_declare_each_governed_collection(contract, tmp_path):
    with pytest.raises(CompositionError, match="without listing it"):
        compose_sources(
            contract,
            [_source(tmp_path, "devices.yaml", {"devices": [_device()]})],
        )


def test_source_cannot_name_unknown_governed_collection(contract, tmp_path):
    with pytest.raises(CompositionError, match="unknown governed"):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "empty.yaml",
                    {},
                    "devcies",
                )
            ],
        )


def test_typed_source_must_contribute_governed_content(contract, tmp_path):
    with pytest.raises(CompositionError, match="contributes no governed"):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "assets.yaml",
                    {"assetz": [{"name": "oven", "properties": {}}]},
                    "assets",
                )
            ],
        )


def test_typed_source_may_declare_an_explicit_empty_collection(contract, tmp_path):
    result = compose_sources(
        contract,
        [_source(tmp_path, "assets.yaml", {"assets": []}, "assets")],
    )

    assert result.parameters["assets"] == []


def test_typed_source_may_only_assert_a_requirement(contract, tmp_path):
    result = compose_sources(
        contract,
        [
            _source(
                tmp_path,
                "require-default.yaml",
                {
                    "_siteops": {
                        "requires": {
                            "dataflowProfiles": [{"name": "default"}],
                        }
                    }
                },
                "dataflows",
            )
        ],
    )

    assert len(result.requirements) == 1
    assert result.requirements[0].identity == ("default",)


def test_seed_identity_cannot_be_written(contract, tmp_path):
    with pytest.raises(CompositionError, match="provider-owned"):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "endpoints.yaml",
                    {
                        "dataflowEndpoints": [
                            {"name": "default", "properties": {}}
                        ]
                    },
                    "dataflowEndpoints",
                )
            ],
        )


def test_cross_source_device_and_endpoint_reference_resolves(contract, tmp_path):
    result = compose_sources(
        contract,
        [
            _source(
                tmp_path,
                "devices.yaml",
                {"devices": [_device()]},
                "devices",
            ),
            _source(
                tmp_path,
                "assets.yaml",
                {"assets": [_asset()]},
                "assets",
            ),
        ],
    )

    reference = result.references[0]
    assert reference.rule_id == "asset-device-endpoint"
    assert reference.target_identity == ("plant-opc",)
    assert reference.target_source == tmp_path / "devices.yaml"


def test_missing_device_reference_is_rejected(contract, tmp_path):
    with pytest.raises(CompositionError) as excinfo:
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "assets.yaml",
                    {"assets": [_asset(device="missing")]},
                    "assets",
                )
            ],
        )
    assert "does not resolve to devices" in str(excinfo.value)
    assert "_siteops.external.devices" in str(excinfo.value)


def test_reference_whitespace_is_rejected_instead_of_normalized(
    contract,
    tmp_path,
):
    with pytest.raises(CompositionError, match="leading or trailing"):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "devices.yaml",
                    {"devices": [_device()]},
                    "devices",
                ),
                _source(
                    tmp_path,
                    "assets.yaml",
                    {"assets": [_asset(device="plant-opc ")]},
                    "assets",
                ),
            ],
        )


def test_missing_required_reference_anchor_is_rejected(contract, tmp_path):
    with pytest.raises(CompositionError, match="requires selector path"):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "assets.yaml",
                    {"assets": [{"name": "oven", "properties": {}}]},
                    "assets",
                )
            ],
        )


@pytest.mark.parametrize(
    ("device_ref", "message"),
    [
        ("plant-opc/opc", "selected a non-mapping"),
        ({"deviceName": "plant-opc"}, "binding path 'endpointName'"),
        (
            {"deviceName": "plant-opc", "endpointName": "   "},
            "must resolve to a non-empty string",
        ),
    ],
)
def test_malformed_device_reference_is_rejected(
    contract,
    tmp_path,
    device_ref,
    message,
):
    with pytest.raises(CompositionError, match=message):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "assets.yaml",
                    {
                        "assets": [
                            {
                                "name": "oven",
                                "properties": {"deviceRef": device_ref},
                            }
                        ]
                    },
                    "assets",
                )
            ],
        )


def test_missing_endpoint_reference_is_rejected(contract, tmp_path):
    with pytest.raises(CompositionError, match="available keys are"):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "devices.yaml",
                    {"devices": [_device()]},
                    "devices",
                ),
                _source(
                    tmp_path,
                    "assets.yaml",
                    {"assets": [_asset(endpoint="missing")]},
                    "assets",
                ),
            ],
        )


def test_external_provider_satisfies_reference(contract, tmp_path):
    result = compose_sources(
        contract,
        [
            _source(
                tmp_path,
                "external.yaml",
                {
                    "_siteops": {
                        "external": {
                            "devices": [
                                {
                                    "name": "plant-opc",
                                    "reason": "Managed elsewhere.",
                                    "expects": {
                                        "properties": {
                                            "endpoints": {
                                                "inbound": {"opc": {}}
                                            }
                                        }
                                    },
                                }
                            ]
                        }
                    }
                },
                "devices",
            ),
            _source(
                tmp_path,
                "assets.yaml",
                {"assets": [_asset()]},
                "assets",
            ),
        ],
    )

    assert result.references[0].external is True


def test_external_missing_member_shape_is_recorded_unverified(
    contract,
    tmp_path,
):
    result = compose_sources(
        contract,
        [
            _source(
                tmp_path,
                "external.yaml",
                {
                    "_siteops": {
                        "external": {
                            "devices": [
                                {
                                    "name": "plant-opc",
                                    "reason": "Managed elsewhere.",
                                }
                            ]
                        }
                    }
                },
                "devices",
            ),
            _source(
                tmp_path,
                "assets.yaml",
                {"assets": [_asset()]},
                "assets",
            ),
        ],
    )

    assert "does not cover" in result.references[0].unverified_reason


@pytest.mark.parametrize("external_first", [False, True])
def test_external_identity_conflicts_with_writer(
    contract,
    tmp_path,
    external_first,
):
    writer = _source(
        tmp_path,
        "devices.yaml",
        {"devices": [_device()]},
        "devices",
    )
    external = _source(
        tmp_path,
        "external.yaml",
        {
            "_siteops": {
                "external": {
                    "devices": [
                        {
                            "name": "plant-opc",
                            "reason": "Managed elsewhere.",
                        }
                    ]
                }
            }
        },
        "devices",
    )
    sources = [external, writer] if external_first else [writer, external]

    with pytest.raises(CompositionError, match="conflicts with"):
        compose_sources(contract, sources)


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (
            {
                "external": {
                    "devices": [
                        {
                            "name": "plant-opc",
                            "reason": "Managed elsewhere.",
                            "expcets": {},
                        }
                    ]
                }
            },
            "unknown key",
        ),
        (
            {
                "requires": {
                    "devices": [
                        {
                            "name": "plant-opc",
                            "becuase": "typo",
                        }
                    ]
                }
            },
            "unknown key",
        ),
    ],
)
def test_advanced_metadata_rejects_unknown_entry_keys(
    contract,
    tmp_path,
    metadata,
    message,
):
    with pytest.raises(CompositionError, match=message):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "metadata.yaml",
                    {"_siteops": metadata},
                    "devices",
                )
            ],
        )


def test_requirement_uses_composite_declaration_identity(contract, tmp_path):
    result = compose_sources(
        contract,
        [
            _source(
                tmp_path,
                "flows.yaml",
                {
                    "_siteops": {
                        "requires": {
                            "dataflows": [
                                {
                                    "profileRef": "production",
                                    "name": "ingest",
                                }
                            ]
                        }
                    },
                    "dataflowProfiles": [
                        {"name": "production", "properties": {}}
                    ],
                    "dataflows": [
                        {
                            "profileRef": "production",
                            "name": "ingest",
                            "properties": {"operations": []},
                        }
                    ],
                },
                "dataflowProfiles",
                "dataflows",
            )
        ],
    )

    assert result.requirements[0].identity == ("production", "ingest")


def test_redundant_requirement_is_rejected(contract, tmp_path):
    with pytest.raises(CompositionError, match="duplicates a dependency"):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "combined.yaml",
                    {
                        "_siteops": {
                            "requires": {
                                "devices": [{"name": "plant-opc"}]
                            }
                        },
                        "devices": [_device()],
                        "assets": [_asset()],
                    },
                    "devices",
                    "assets",
                )
            ],
        )


def test_unverified_reference_is_recorded(contract, tmp_path):
    result = compose_sources(
        contract,
        [
            _source(
                tmp_path,
                "flows.yaml",
                {
                    "dataflows": [
                        {
                            "name": "publish",
                            "properties": {
                                "operations": [
                                    {
                                        "sourceSettings": {
                                            "assetRef": "oven"
                                        }
                                    }
                                ]
                            },
                        }
                    ]
                },
                "dataflows",
            )
        ],
    )

    unverified = [
        reference
        for reference in result.references
        if reference.rule_id == "dataflow-source-asset"
    ]
    assert len(unverified) == 1
    assert unverified[0].unverified_reason


def test_siteops_metadata_is_rejected_inside_resource_entry(contract, tmp_path):
    with pytest.raises(CompositionError, match="allowed only at .* root"):
        compose_sources(
            contract,
            [
                _source(
                    tmp_path,
                    "devices.yaml",
                    {
                        "devices": [
                            {
                                "name": "plant-opc",
                                "_siteops": {"external": {}},
                                "properties": {},
                            }
                        ]
                    },
                    "devices",
                )
            ],
        )
