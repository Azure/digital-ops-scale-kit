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
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import yaml

ROOT = Path(__file__).resolve().parents[1]
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


def cases() -> list[Case]:
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
    for sample in ("resource-set-basic", "resource-set-composition"):
        for custom in (False, True):
            selected.append(Case(
                f"deploy-{sample}-{'custom' if custom else 'default'}", "deploy",
                {"manifest": f"samples/{sample}/manifest.yaml", "dryRun": True,
                 "selector": "country=US" if custom else " "},
                f"environment=sample,sample={sample}" + (",country=US" if custom else ""),
            ))
    source = yaml.safe_load((ROOT / PIPELINES["integration"]).read_text(encoding="utf-8"))
    phases = next(parameter["values"] for parameter in source["parameters"]
                  if parameter["name"] == "manifest")
    if not phases or len(phases) != len(set(phases)) or any(
        not isinstance(phase, str) or not re.fullmatch(r"[a-z0-9-]+", phase)
        for phase in phases
    ):
        raise PreviewError("The integration phase inventory is invalid.")
    selected.extend(Case(f"integration-{phase}", "integration", {"manifest": phase})
                    for phase in phases)
    selected.append(Case("integration-wif-session", "integration", {"keepAzSessionActive": True}))
    for environment in ("staging", "prod"):
        selected.append(Case(f"integration-{environment}", "integration",
                             {"environment": environment, "skipCleanup": True}))
    for label, options in (
        ("local", {"enableCache": True, "installDev": False}),
        ("development", {"enableCache": True, "installDev": True}),
        ("external", {"enableCache": False, "siteopsSource": "https://example.invalid/siteops.whl"}),
    ):
        override = yaml.safe_dump({
            "trigger": "none", "pr": "none", "pool": {"vmImage": "ubuntu-24.04"},
            "jobs": [{"job": "setup_preview", "steps": [{
                "template": "templates/setup-siteops.yaml", "parameters": options,
            }]}],
        })
        selected.append(Case(f"setup-{label}", "ci", options, override=override))
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
    if case.override:
        _one(nodes, "job", "setup_preview")
        installation = _one(nodes, "displayName", "Install Site Ops")
        environment = _mapping(installation.get("env", {}))
        if not _boolean(environment.get("INSTALL_DEV"), case.parameters.get("installDev", False)):
            raise PreviewError("The setup template selected different development dependencies.")
        if environment.get("SITEOPS_SOURCE", "") != case.parameters.get("siteopsSource", ""):
            raise PreviewError("The setup template selected a different installation source.")
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
                or not _boolean(task_env.get("DRY_RUN"), case.parameters.get("dryRun", False))
                or task_env.get("MANIFEST") != case.parameters.get(
                    "manifest", "manifests/aio-install/manifest.yaml")):
            raise PreviewError("The expanded deployment inputs differ from the requested case.")
        body = inputs.get("inlineScript", "")
        if not isinstance(body, str) or "--yes" not in body:
            raise PreviewError("The expanded deployment omitted unattended consent.")
    elif (task_env.get("MANIFEST") != case.parameters.get("manifest", "all")
          or not _boolean(task_env.get("INTEGRATION_SKIP_CLEANUP"),
                          case.parameters.get("skipCleanup", False))):
        raise PreviewError("The expanded integration inputs differ from the requested case.")


def _repository(value: str) -> str:
    if not isinstance(value, str):
        raise PreviewError("Select an HTTPS source repository without credentials.")
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise PreviewError("Select an HTTPS source repository without credentials.")
    return value.rstrip("/").removesuffix(".git").casefold()


def qualify(
    client: AdoClient, pipeline_ids: dict[str, int], repository: str, ref: str, commit: str,
    connections: dict[str, str], groups: dict[str, str],
) -> dict:
    """Read the chosen definitions and preview only that exact repository candidate."""
    if (not re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", ref) or ".." in ref
            or not re.fullmatch(r"[0-9a-f]{40}", commit)):
        raise PreviewError("Select a branch and its exact full source commit.")
    if (not isinstance(pipeline_ids, dict) or set(pipeline_ids) != set(PIPELINES)
            or any(type(value) is not int or value <= 0 for value in pipeline_ids.values())
            or len(set(pipeline_ids.values())) != len(PIPELINES)):
        raise PreviewError("Select distinct positive CI, deploy and integration pipeline IDs.")
    for mapping in (connections, groups):
        if not isinstance(mapping, dict) or set(mapping) != set(ENVIRONMENTS) or any(
            not isinstance(value, str) or not value.strip()
            or any(character in value for character in "\r\n") for value in mapping.values()
        ):
            raise PreviewError("Provide explicit service connection and variable group mappings.")
    expected_repository = _repository(repository)
    for name, path in PIPELINES.items():
        try:
            definition = client.request(pipeline_ids[name])
            source = _mapping(definition.get("repository"))
            process = _mapping(definition.get("process"))
            filename = process.get("yamlFilename")
            if (definition.get("id") != pipeline_ids[name]
                    or _repository(source.get("url", "")) != expected_repository
                    or not isinstance(filename, str) or filename.lstrip("/") != path):
                raise PreviewError("The definition does not match the candidate repository and YAML.")
        except PreviewError as error:
            raise PreviewError(f"{name} definition: {error}") from None
    receipts = []
    for case in cases():
        parameters = dict(case.parameters) if case.override is None else {}
        if case.pipeline != "ci":
            parameters.update(serviceConnections=connections, secretGroups=groups)
        payload = {
            "previewRun": True,
            "resources": {"repositories": {"self": {"refName": ref, "version": commit}}},
            "templateParameters": parameters,
        }
        if case.override:
            payload["yamlOverride"] = case.override
        print(f"Previewing {case.name}.", flush=True)
        try:
            document = client.request(pipeline_ids[case.pipeline], preview=payload)
            text = document.get("finalYaml")
            if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_RESPONSE:
                raise PreviewError("The preview service returned no bounded final YAML.")
            validate_expansion(case, text, connections)
        except PreviewError as error:
            raise PreviewError(f"{case.name}: {error}") from None
        receipts.append({"case": case.name, "status": "passed",
                         "expandedSha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    return {"sourceCommit": commit, "status": "passed", "cases": receipts}


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
            client, json.loads(os.environ["ADO_PREVIEW_PIPELINE_IDS"]),
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
