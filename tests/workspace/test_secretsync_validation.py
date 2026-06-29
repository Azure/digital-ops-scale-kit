"""Workspace tests for the sync-secrets chaining input contract.

The Bicep template groups `secrets:` entries by `kubernetesSecretName`
and does not enforce uniqueness of `(kubernetesSecretName,
kubernetesSecretKey)` pairs at deploy time. A duplicate pair in a
committed scalekit input would silently write to the same Kubernetes
Secret slot. These tests guard the contract:

- `secretName` is unique across the array.
- `(kubernetesSecretName, kubernetesSecretKey)` is globally unique,
  with each field defaulting to `secretName` when not set.
- No two `kubernetesSecretName` values are close-enough near-matches
  to suggest a typo, since a typo would silently split a multi-key
  Secret into two single-key Secrets.
"""

import difflib
from pathlib import Path

import yaml


def _collect_sync_secrets_inputs(workspace: Path) -> list[Path]:
    """Find every workspace YAML that declares a sync-secrets `secrets:`
    array. Walks all YAML and lets `_load_secrets_array` reject
    non-matching files structurally so manifests that use all-default
    naming (no `kubernetesSecretName` / `kubernetesSecretKey` overrides)
    are not silently skipped.
    """
    matches: list[Path] = []
    for path in workspace.rglob("*.yaml"):
        if not path.is_file():
            continue
        matches.append(path)
    return sorted(matches)


def _load_secrets_array(yaml_path: Path) -> list[dict] | None:
    """Parse `secrets:` from a chaining input. Returns None when the
    file has no `secrets:` array of dicts in scope. YAML validity is
    covered by other workspace tests.
    """
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    secrets = data.get("secrets")
    if not isinstance(secrets, list):
        return None
    return [s for s in secrets if isinstance(s, dict) and "secretName" in s]


def _effective_k8s_name(entry: dict) -> str:
    """`kubernetesSecretName`, defaulting to `secretName` per the
    template's `??` fallback. Only a missing key coalesces; an
    explicit empty string is preserved so `_check_no_empty_overrides`
    can surface it.
    """
    value = entry.get("kubernetesSecretName")
    return entry["secretName"] if value is None else value


def _effective_k8s_key(entry: dict) -> str:
    """`kubernetesSecretKey`, defaulting to `secretName` per the
    template's `??` fallback. Only a missing key coalesces; an
    explicit empty string is preserved.
    """
    value = entry.get("kubernetesSecretKey")
    return entry["secretName"] if value is None else value


# 0.92 catches 1-char typos on names ~15 chars and up while leaving
# legitimate sibling names that share a prefix and suffix below the
# bar (e.g. `app-db-credentials` vs `app-mqtt-credentials`, ~0.91).
# If a future bundle name forces a false positive, add an explicit
# escape (e.g. allowlist entry or sibling-aware ratio) rather than
# lowering the threshold further.
_NEAR_MATCH_RATIO = 0.92


class TestSyncSecretsInputContract:
    """Committed sync-secrets chaining inputs satisfy the input contract.
    Customer-authored manifests are not in scope; they follow the
    Constraints section in `docs/secret-sync.md`.
    """

    def test_each_inputs_file_passes_contract(self, workspace):
        inputs = _collect_sync_secrets_inputs(workspace)
        failures: list[str] = []
        sync_secrets_files: list[Path] = []
        for path in inputs:
            entries = _load_secrets_array(path)
            if entries is None:
                continue
            sync_secrets_files.append(path)
            failures.extend(self._check_no_empty_overrides(path, entries))
            failures.extend(self._check_unique_secret_names(path, entries))
            failures.extend(self._check_unique_pairs(path, entries))
            failures.extend(self._check_no_near_miss_names(path, entries))

        assert sync_secrets_files, (
            "Expected at least one workspace YAML with a `secrets:` array "
            "carrying sync-secrets entries. Found none. If the sync-secrets "
            "sample moved or was removed, update the discovery filter."
        )
        assert not failures, "\n\n".join(failures)

    @staticmethod
    def _check_no_empty_overrides(path: Path, entries: list[dict]) -> list[str]:
        """The template's `??` only coalesces null, so an empty override
        deploys as an empty resource or key name and fails at ARM time.
        Surface that as a contract violation early.
        """
        failures: list[str] = []
        for i, entry in enumerate(entries):
            for field in ("kubernetesSecretName", "kubernetesSecretKey"):
                if field in entry and not entry[field]:
                    failures.append(
                        f"{path}: entry at index {i} sets `{field}` to an "
                        f"empty value. Remove the field to use the "
                        f"`secretName` default rather than deploying an "
                        f"empty name or key."
                    )
        return failures

    @staticmethod
    def _check_unique_secret_names(path: Path, entries: list[dict]) -> list[str]:
        seen: dict[str, int] = {}
        failures: list[str] = []
        for i, entry in enumerate(entries):
            name = entry["secretName"]
            if name in seen:
                failures.append(
                    f"{path}: `secretName` '{name}' appears at indices "
                    f"{seen[name]} and {i}. Each `secretName` must be unique "
                    f"because it identifies one Key Vault secret per entry."
                )
                continue
            seen[name] = i
        return failures

    @staticmethod
    def _check_unique_pairs(path: Path, entries: list[dict]) -> list[str]:
        seen: dict[tuple[str, str], int] = {}
        failures: list[str] = []
        for i, entry in enumerate(entries):
            pair = (_effective_k8s_name(entry), _effective_k8s_key(entry))
            if pair in seen:
                failures.append(
                    f"{path}: `(kubernetesSecretName, kubernetesSecretKey)` "
                    f"pair {pair} appears at indices {seen[pair]} and {i}. "
                    f"Two entries claiming the same pair would write to the "
                    f"same Kubernetes Secret slot and the cluster-side "
                    f"reconcile order would decide which value wins."
                )
                continue
            seen[pair] = i
        return failures

    @staticmethod
    def _check_no_near_miss_names(path: Path, entries: list[dict]) -> list[str]:
        names = sorted({_effective_k8s_name(e) for e in entries})
        if len(names) < 2:
            return []
        failures: list[str] = []
        for name in names:
            others = [n for n in names if n != name]
            for match in difflib.get_close_matches(name, others, n=len(others), cutoff=_NEAR_MATCH_RATIO):
                if match < name:
                    continue
                failures.append(
                    f"{path}: `kubernetesSecretName` values '{name}' and "
                    f"'{match}' are near-matches (difflib ratio >= "
                    f"{_NEAR_MATCH_RATIO}). If the similarity is "
                    f"intentional, rename one to differ more. If one is a "
                    f"typo, the two entries silently split into separate "
                    f"single-key Kubernetes Secrets instead of grouping "
                    f"into one multi-key Secret."
                )
        return failures
