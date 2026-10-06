#!/usr/bin/env python3
"""Preview committed Azure Pipelines templates without queueing builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import yaml
from source_snapshot import SourceSnapshotError, _git, validate_repository

ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION_PIPELINE = ".pipelines/validate-pipelines.yaml"
PIPELINES = {
    "ci": ".pipelines/ci.yaml",
    "deploy": ".pipelines/deploy.yaml",
    "integration": ".pipelines/integration-test.yaml",
}
MAX_RESPONSE = 4 * 1024 * 1024
ENVIRONMENTS = ("dev", "staging", "prod")


class PreviewError(Exception):
    """A fixed diagnostic suitable for a public validation log."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PreviewError("The preview service returned an unexpected redirect.")


class AdoClient:
    def __init__(self, collection: str, project: str, token: str) -> None:
        if not re.fullmatch(r"https://dev\.azure\.com/[A-Za-z0-9][A-Za-z0-9-]{0,63}/?", collection):
            raise PreviewError("Select an HTTPS Azure DevOps organization URL.")
        if not project or any(character in project for character in "/\\?#\r\n"):
            raise PreviewError("Select one Azure DevOps project.")
        if not token or any(character in token for character in "\r\n"):
            raise PreviewError("A scoped pipeline access token is required.")
        self.base = collection.rstrip("/") + "/" + quote(project, safe="")
        self.token = token
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=context))

    def request(self, pipeline_id: int, *, preview: dict | None = None) -> dict:
        """Allow only definition reads and the dedicated preview endpoint."""
        if type(pipeline_id) is not int or pipeline_id <= 0:
            raise PreviewError("Pipeline IDs must be positive integers.")
        if preview is not None and preview.get("previewRun") is not True:
            raise PreviewError("Template validation requires previewRun=true.")
        path = (
            f"/_apis/build/definitions/{pipeline_id}"
            if preview is None else f"/_apis/pipelines/{pipeline_id}/preview"
        )
        request = Request(
            self.base + path + "?api-version=7.1",
            data=None if preview is None else json.dumps(preview).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                     "Content-Type": "application/json"},
            method="GET" if preview is None else "POST",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status != 200:
                    raise PreviewError("The pipeline service returned an unexpected status.")
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise PreviewError("The pipeline service response exceeded its size limit.")
            document = json.loads(raw)
        except HTTPError as error:
            raise PreviewError(f"The pipeline service rejected the request (HTTP {error.code}).") from None
        except (URLError, OSError, ValueError, RecursionError):
            raise PreviewError("The pipeline service response could not be read.") from None
        if not isinstance(document, dict):
            raise PreviewError("The pipeline service returned an unsupported document.")
        return document


@dataclass(frozen=True)
class Case:
    name: str
    pipeline: str
    parameters: dict
    selector: str = ""
    override: str | None = None


def cases(integration_source: str | None = None) -> list[Case]:
    """Cover parameter branches rather than every possible parameter combination."""
    selected = [Case("ci", "ci", {})]
    for environment in ENVIRONMENTS:
        for dry_run in (False, True):
            selected.append(Case(
                f"deploy-{environment}-{'plan' if dry_run else 'apply'}", "deploy",
                {"environment": environment, "dryRun": dry_run},
                f"environment={environment}",
            ))
    selected.append(Case(
        "deploy-custom-selector", "deploy", {"selector": "country=US", "dryRun": True},
        "environment=dev,country=US",
    ))
    selected.append(Case(
        "deploy-wif-session", "deploy", {"keepAzSessionActive": True}, "environment=dev",
    ))
    selected.append(Case("deploy-site-file", "deploy", {"siteFile": "operator/site.yaml", "dryRun": True}))
    for sample in ("resource-set-basic", "resource-set-composition"):
        for custom in (False, True):
            selected.append(Case(
                f"deploy-{sample}-{'custom' if custom else 'default'}", "deploy",
                {"manifest": f"samples/{sample}/manifest.yaml", "dryRun": True,
                 "selector": "country=US" if custom else " "},
                f"environment=sample,sample={sample}" + (",country=US" if custom else ""),
            ))
    try:
        source = yaml.safe_load(
            integration_source if integration_source is not None
            else (ROOT / PIPELINES["integration"]).read_text(encoding="utf-8")
        )
    except yaml.YAMLError:
        raise PreviewError("The integration phase inventory is invalid.") from None
    parameters = source.get("parameters") if isinstance(source, dict) else None
    inventories = [
        parameter.get("values") for parameter in parameters
        if isinstance(parameter, dict) and parameter.get("name") == "manifest"
    ] if isinstance(parameters, list) else []
    phases = inventories[0] if len(inventories) == 1 else None
    if not isinstance(phases, list) or not phases or any(
        not isinstance(phase, str) or not re.fullmatch(r"[a-z0-9-]+", phase)
        for phase in phases
    ) or len(phases) != len(set(phases)):
        raise PreviewError("The integration phase inventory is invalid.")
    selected.extend(Case(f"integration-{phase}", "integration", {"manifest": phase})
                    for phase in phases)
    selected.append(Case("integration-wif-session", "integration", {"keepAzSessionActive": True}))
    for environment in ("staging", "prod"):
        selected.append(Case(f"integration-{environment}", "integration",
                             {"environment": environment, "skipCleanup": True}))
    for label, options in (
        ("development", {"enableCache": True, "installDev": True}),
        ("external", {"enableCache": False, "siteopsSource": "https://example.invalid/siteops.whl"}),
        ("release", {"enableCache": False, "release": "v1.0.0b7", "sourceCommit": "a" * 40}),
    ):
        override = yaml.safe_dump({
            "trigger": "none", "pr": "none", "pool": {"vmImage": "ubuntu-24.04"},
            "jobs": [{"job": "setup_preview", "steps": [{
                "template": "templates/setup-siteops.yaml", "parameters": options,
            }]}],
        })
        selected.append(Case(f"setup-{label}", "ci", options, override=override))
    for label, targeting in (("selector", {"selector": "environment=dev"}), ("site-file", {"siteFile": "operator/site.yaml"})):
        options = {
            "workspace": "deployment", "manifest": "manifests/custom.yaml",
            "release": "v1.0.0b7", "sourceCommit": "a" * 40, **targeting,
        }
        override = yaml.safe_dump({
            "trigger": "none", "pr": "none", "pool": {"vmImage": "ubuntu-24.04"},
            "variables": {"SITE_OVERRIDES": ""},
            "stages": [{"template": "templates/siteops-validate.yaml", "parameters": options}],
        })
        selected.append(Case(f"consumer-validate-{label}", "ci", options, override=override))
    return selected


def _nodes(value: object):
    pending = [(value, 0)]
    visited = 0
    while pending:
        node, depth = pending.pop()
        visited += 1
        if visited > 20000 or depth > 64:
            raise PreviewError("The expanded pipeline exceeds its supported structure limits.")
        if isinstance(node, dict):
            yield node
            pending.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            pending.extend((child, depth + 1) for child in node)


def _one(nodes: list[dict], key: str, value: str) -> dict:
    matches = [node for node in nodes if node.get(key) == value]
    if len(matches) != 1:
        raise PreviewError("The expanded pipeline is missing a required unique step or job.")
    return matches[0]


def _boolean(value: object, expected: bool) -> bool:
    return value is expected or (
        isinstance(value, str) and value.casefold() == str(expected).casefold()
    )


def _mapping(value: object) -> dict:
    if not isinstance(value, dict):
        raise PreviewError("The pipeline service returned an unsupported mapping.")
    return value


def validate_expansion(case: Case, text: str, connections: dict[str, str]) -> None:
    """Check the service expansion without printing private YAML or evaluating expressions."""
    try:
        document = yaml.safe_load(text)
        nodes = list(_nodes(document))
    except (yaml.YAMLError, RecursionError):
        raise PreviewError("The expanded pipeline is not supported YAML.") from None
    if not isinstance(document, dict) or not nodes:
        raise PreviewError("The expanded pipeline is empty.")
    if any("template" in node for node in nodes):
        raise PreviewError("The service returned unexpanded template references.")
    if case.name.startswith("consumer-validate-"):
        _one(nodes, "job", "siteops_validate")
        if any(node.get("task", "").startswith("AzureCLI") or "environment" in node for node in nodes):
            raise PreviewError("Structural consumer validation must not acquire Azure deployment authority.")
        installation = _one(nodes, "displayName", "Install Site Ops")
        environment = _mapping(installation.get("env"))
        if (environment.get("SITEOPS_RELEASE") != case.parameters["release"]
                or environment.get("SITEOPS_SOURCE_COMMIT") != case.parameters["sourceCommit"]):
            raise PreviewError("Consumer validation changed its selected release.")
        validation = _one(nodes, "displayName", "Validate caller content")
        environment = _mapping(validation.get("env"))
        expected = {
            "WORKSPACE": case.parameters["workspace"], "MANIFEST": case.parameters["manifest"],
            "SELECTOR": case.parameters.get("selector", ""), "SITE_FILE": case.parameters.get("siteFile", ""),
        }
        if any(environment.get(key) != value for key, value in expected.items()):
            raise PreviewError("Consumer validation changed its caller content or targeting.")
        return
    if case.override:
        _one(nodes, "job", "setup_preview")
        installation = _one(nodes, "displayName", "Install Site Ops")
        environment = _mapping(installation.get("env", {}))
        if not _boolean(environment.get("INSTALL_DEV"), case.parameters.get("installDev", False)):
            raise PreviewError("The setup template selected different development dependencies.")
        if environment.get("SITEOPS_SOURCE", "") != case.parameters.get("siteopsSource", ""):
            raise PreviewError("The setup template selected a different installation source.")
        if (environment.get("SITEOPS_RELEASE", "") != case.parameters.get("release", "")
                or environment.get("SITEOPS_SOURCE_COMMIT", "") != case.parameters.get("sourceCommit", "")):
            raise PreviewError("The setup template selected a different release identity.")
        caches = [node for node in nodes if node.get("displayName") == "Cache pip packages"]
        if len(caches) != int(case.parameters["enableCache"]):
            raise PreviewError("The setup cache condition did not match the requested case.")
        return
    if case.pipeline == "ci":
        for job in ("lint", "test", "validate"):
            _one(nodes, "job", job)
        fixture = _one(nodes, "displayName", "Prepare native Linux uv fixtures")
        body = fixture.get("script", _mapping(fixture.get("inputs", {})).get("script", ""))
        if not isinstance(body, str) or "tests/fixtures/prepare-native-uv.sh" not in body:
            raise PreviewError("The CI expansion omitted required native fixture preparation.")
        _one(nodes, "displayName", "Run unit tests")
        return
    environment = case.parameters.get("environment", "dev")
    job = _one(nodes, "deployment", "siteops_deploy" if case.pipeline == "deploy" else "integration_test")
    actual_environment = job.get("environment")
    if isinstance(actual_environment, dict):
        actual_environment = actual_environment.get("name")
    if actual_environment != environment:
        raise PreviewError("The expanded deployment selected a different approval environment.")
    task = _one(
        nodes, "displayName",
        "Prepare executable plan and deploy" if case.pipeline == "deploy" else "Run integration tests",
    )
    inputs = _mapping(task.get("inputs", {}))
    if inputs.get("azureSubscription") != connections[environment] or inputs.get("scriptType") != "bash":
        raise PreviewError("The expanded task selected a different service connection or shell.")
    if not _boolean(inputs.get("keepAzSessionActive", False), case.parameters.get("keepAzSessionActive", False)):
        raise PreviewError("The expanded task changed the requested WIF session refresh setting.")
    task_env = _mapping(task.get("env", {}))
    if case.pipeline == "deploy":
        if (task_env.get("SELECTOR") != case.selector
                or task_env.get("SITE_FILE", "") != case.parameters.get("siteFile", "")
                or not _boolean(task_env.get("DRY_RUN"), case.parameters.get("dryRun", False))
                or task_env.get("MANIFEST") != case.parameters.get(
                    "manifest", "manifests/aio-install/manifest.yaml")):
            raise PreviewError("The expanded deployment inputs differ from the requested case.")
        body = inputs.get("inlineScript", "")
        if not isinstance(body, str) or "--yes" not in body or (case.parameters.get("siteFile") and "--site-file" not in body):
            raise PreviewError("The expanded deployment omitted unattended consent.")
    elif (task_env.get("MANIFEST") != case.parameters.get("manifest", "all")
          or not _boolean(task_env.get("INTEGRATION_SKIP_CLEANUP"),
                          case.parameters.get("skipCleanup", False))):
        raise PreviewError("The expanded integration inputs differ from the requested case.")


def _repository(value: str) -> str:
    if not isinstance(value, str):
        raise PreviewError("Select an HTTPS source repository without credentials.")
    try:
        url = urlsplit(value)
    except ValueError:
        raise PreviewError("Select an HTTPS source repository without credentials.") from None
    organization = url.path.split("/")[1] if url.path.startswith("/") else ""
    organization_hint = (
        url.hostname == "dev.azure.com" and bool(organization)
        and url.username is not None and url.username.casefold() == organization.casefold()
    )
    if (url.scheme != "https" or not url.hostname or url.password is not None
            or (url.username is not None and not organization_hint)
            or url.query or url.fragment):
        raise PreviewError("Select an HTTPS source repository without credentials.")
    canonical = urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
    return canonical.rstrip("/").removesuffix(".git").casefold()


def source_documents(commit: str) -> dict[str, str]:
    """Read bounded raw Git blobs after admitting the exact clean checkout."""
    try:
        validate_repository(ROOT, commit)
        documents = {}
        for name, path in PIPELINES.items():
            identity = f"{commit}:{path}"
            size = _git(ROOT, ["--no-replace-objects", "cat-file", "-s", identity])
            if size.returncode or not size.stdout.strip().isdigit():
                raise PreviewError("A committed pipeline file could not be inspected.")
            length = int(size.stdout.strip())
            if not 0 < length <= MAX_RESPONSE:
                raise PreviewError("A committed pipeline file is empty or too large.")
            result = _git(ROOT, ["--no-replace-objects", "cat-file", "blob", identity])
            if result.returncode or len(result.stdout) != length:
                raise PreviewError("A committed pipeline file could not be read completely.")
            documents[name] = result.stdout.decode("utf-8")
        return documents
    except (SourceSnapshotError, ValueError):
        raise PreviewError("The exact clean source checkout could not be read.") from None


def qualify(
    client: AdoClient, pipeline_id: int, repository: str, ref: str, commit: str,
    connections: dict[str, str], groups: dict[str, str],
) -> dict:
    """Use this qualification definition to preview the exact committed entry points."""
    if (not re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", ref) or ".." in ref
            or not re.fullmatch(r"[0-9a-f]{40}", commit)):
        raise PreviewError("Select a branch and its exact full source commit.")
    if type(pipeline_id) is not int or pipeline_id <= 0:
        raise PreviewError("Select the positive qualification pipeline ID.")
    for mapping in (connections, groups):
        if not isinstance(mapping, dict) or set(mapping) != set(ENVIRONMENTS) or any(
            not isinstance(value, str) or not value.strip()
            or any(character in value for character in "\r\n") for value in mapping.values()
        ):
            raise PreviewError("Provide explicit service connection and variable group mappings.")
    expected_repository = _repository(repository)
    documents = source_documents(commit)
    definition = client.request(pipeline_id)
    source = _mapping(definition.get("repository"))
    process = _mapping(definition.get("process"))
    filename = process.get("yamlFilename")
    if (definition.get("id") != pipeline_id
            or _repository(source.get("url", "")) != expected_repository
            or not isinstance(filename, str) or filename.lstrip("/") != QUALIFICATION_PIPELINE):
        raise PreviewError("The qualification definition does not match the candidate repository and YAML.")
    receipts = []
    selected = cases(documents["integration"])
    for case in selected:
        parameters = dict(case.parameters) if case.override is None else {}
        if case.pipeline != "ci":
            parameters.update(serviceConnections=connections, secretGroups=groups)
        payload = {
            "previewRun": True,
            "resources": {"repositories": {"self": {"refName": ref, "version": commit}}},
            "templateParameters": parameters,
            "yamlOverride": case.override if case.override is not None else documents[case.pipeline],
        }
        print(f"Previewing {case.name}.", flush=True)
        try:
            document = client.request(pipeline_id, preview=payload)
            text = document.get("finalYaml")
            if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_RESPONSE:
                raise PreviewError("The preview service returned no bounded final YAML.")
            validate_expansion(case, text, connections)
        except PreviewError as error:
            raise PreviewError(f"{case.name}: {error}") from None
        request_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        receipts.append({"case": case.name, "status": "passed",
                         "inputSha256": hashlib.sha256(request_bytes).hexdigest(),
                         "expandedSha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    return {"sourceCommit": commit, "status": "passed",
            "expectedCases": [case.name for case in selected], "cases": receipts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New path for the safe JSON receipt.")
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise PreviewError("Choose a new output receipt path.")
        client = AdoClient(
            os.environ["SYSTEM_COLLECTIONURI"], os.environ["SYSTEM_TEAMPROJECTID"],
            os.environ["SYSTEM_ACCESSTOKEN"],
        )
        report = qualify(
            client, int(os.environ["SYSTEM_DEFINITIONID"]),
            os.environ["BUILD_REPOSITORY_URI"], os.environ["BUILD_SOURCEBRANCH"],
            os.environ["BUILD_SOURCEVERSION"], json.loads(os.environ["ADO_PREVIEW_CONNECTIONS"]),
            json.loads(os.environ["ADO_PREVIEW_GROUPS"]),
        )
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    except (PreviewError, KeyError, ValueError, OSError, TypeError, RecursionError) as error:
        message = str(error) if isinstance(error, PreviewError) else "Pipeline preview configuration or receipt is invalid."
        print(message, file=sys.stderr)
        return 1
    print(f"Pipeline template preview passed for {len(report['cases'])} cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
