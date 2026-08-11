"""Tests for the engine's YAML loader.

A duplicate mapping key is the failure this loader exists to stop. YAML keeps
the last one, so the operator's first block is discarded before anything
downstream can see it, and the deploy reports success having written something
other than what was authored.
"""

import pytest
import yaml

from siteops import yamlio


class TestDuplicateKeys:
    def test_a_duplicated_key_is_rejected(self):
        with pytest.raises(yamlio.DuplicateKeyError):
            yamlio.load("name: first\nname: second\n")

    def test_the_message_names_the_key_and_the_consequence(self):
        with pytest.raises(yamlio.DuplicateKeyError) as excinfo:
            yamlio.load("properties:\n  a: 1\nproperties:\n  b: 2\n")
        message = str(excinfo.value)
        assert "'properties'" in message
        assert "discarded" in message

    def test_a_duplicate_nested_in_a_mapping_is_rejected(self):
        """The guard applies at every depth, not only at the document root."""
        document = "site:\n  properties:\n    dataflows: a\n    dataflows: b\n"
        assert yaml.safe_load(document)["site"]["properties"] == {"dataflows": "b"}
        with pytest.raises(yamlio.DuplicateKeyError):
            yamlio.load(document)

    def test_a_duplicate_inside_a_sequence_entry_is_rejected(self):
        """Declaration entries are sequence items, so the guard has to reach them."""
        with pytest.raises(yamlio.DuplicateKeyError):
            yamlio.load("dataflows:\n  - name: a\n    name: b\n")

    def test_the_same_key_in_sibling_mappings_is_allowed(self):
        """Two entries may each carry `name`. Only a repeat within one mapping is wrong."""
        loaded = yamlio.load("dataflows:\n  - name: a\n  - name: b\n")
        assert [entry["name"] for entry in loaded["dataflows"]] == ["a", "b"]

    def test_the_error_is_a_yaml_error(self):
        """A caller that already tolerates a parse error keeps its behavior.

        Two engine call sites deliberately swallow `yaml.YAMLError`, one to
        defer reporting to a path with more context and one to answer a
        yes-or-no question about a file. Neither should start raising.
        """
        assert issubclass(yamlio.DuplicateKeyError, yaml.YAMLError)


class TestOrdinaryDocuments:
    def test_a_well_formed_document_loads(self):
        loaded = yamlio.load("kind: Site\nproperties:\n  resourceSets:\n    dataflows: none\n")
        assert loaded["properties"]["resourceSets"]["dataflows"] == "none"

    def test_an_empty_document_loads_as_none(self):
        """Matches `yaml.safe_load`, since callers apply `or {}` to the result."""
        assert yamlio.load("") is None

    def test_malformed_yaml_still_raises(self):
        with pytest.raises(yaml.YAMLError):
            yamlio.load("a:\n  b: 1\n    c: 2\n")

    def test_a_merge_key_still_resolves(self):
        """`<<` appears once per mapping, so the guard must not break inheritance."""
        loaded = yamlio.load(
            "base: &base\n  a: 1\nchild:\n  <<: *base\n  b: 2\n"
        )
        assert loaded["child"] == {"a": 1, "b": 2}

    def test_a_merge_key_override_is_allowed(self):
        """An explicit key wins over one pulled in by `<<`, which is not a duplicate.

        Checking for duplicates after the merge is applied would reject this,
        since the merged key and the explicit one both land in the same mapping.
        """
        loaded = yamlio.load(
            "base: &base\n  a: 1\n  b: 2\nchild:\n  <<: *base\n  a: 99\n"
        )
        assert loaded["child"] == {"a": 99, "b": 2}
