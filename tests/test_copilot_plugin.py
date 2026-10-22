"""Keep the Copilot plugin's manifests, links and skill commands aligned with the CLI."""

import json
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

from siteops import cli

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "siteops"
MARKETPLACE = ROOT / ".github" / "plugin" / "marketplace.json"
AGENT_PLUGINS_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MANIFEST_FIELDS = {
    "$schema", "name", "version", "description", "author", "homepage",
    "repository", "license", "keywords", "extensions",
}
SKILL_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME = r"[a-z0-9]+(-[a-z0-9]+)*"
REPOSITORY_URL = "https://github.com/Azure/digital-ops-scale-kit"
ARM = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/plant/providers"
PLACEHOLDERS = {
    "<source>": "official",
    "<release>": "v1.0.0b7",
    "<owner>": "contoso",
    "<repository>": "content",
    "<cluster-resource-ID>": f"{ARM}/Microsoft.Kubernetes/connectedClusters/plant-one",
    "<instance-resource-ID>": f"{ARM}/Microsoft.IoTOperations/instances/plant-one",
    "<project>": "./my factory",
    "<manifest>": "aio-install",
    "<site-name>": "plant-two",
    "<other-site-name>": "plant-three",
    "<aio-release>": "2609",
}


def _skills():
    return sorted((PLUGIN / "skills").glob("*/SKILL.md"))


def _skill_documents(skill):
    return sorted(skill.parent.rglob("*.md"))


def _front_matter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{path.parent.name} needs YAML front matter"
    return yaml.safe_load(match.group(1)), text[match.end():]


def _commands(documents):
    commands = []
    for path in documents:
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r"```text\n(.*?)```", text, re.DOTALL):
            commands += [line.strip() for line in block.splitlines() if line.strip().startswith("siteops ")]
        prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        commands += re.findall(r"`(siteops [^`]+)`", prose)
    return commands


def _siteops_commands():
    return _commands(path for skill in _skills() for path in _skill_documents(skill))


def _argv(command):
    def substitute(match):
        assert match.group(0) in PLACEHOLDERS, f"Add a sample value for {match.group(0)}."
        return PLACEHOLDERS[match.group(0)]

    return shlex.split(re.sub(r"<[^>]+>", substitute, command))


def _slug(heading):
    heading = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return heading.replace(" ", "-")


def _anchors(path):
    text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return {_slug(match) for match in re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE)}


def _links(path):
    text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return re.findall(r"\]\(([^)\s]+)\)", text)


def _plugin_documents():
    return sorted(PLUGIN.rglob("*.md"))


def test_manifest_uses_the_portable_plugin_format():
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["$schema"] == AGENT_PLUGINS_SCHEMA
    assert set(manifest) <= MANIFEST_FIELDS
    assert re.fullmatch(NAME, manifest["name"])


def test_marketplace_lists_the_plugin_as_published():
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    (entry,) = [plugin for plugin in marketplace["plugins"] if plugin["name"] == manifest["name"]]
    assert (MARKETPLACE.parents[2] / entry["source"]).resolve() == PLUGIN
    assert (entry["version"], entry["description"]) == (manifest["version"], manifest["description"])


def test_each_skill_follows_the_agent_skills_format():
    assert _skills()
    for path in _skills():
        front, body = _front_matter(path)
        assert set(front) <= SKILL_FIELDS, f"{path.parent.name} has unknown front matter"
        assert front["name"] == path.parent.name
        assert len(front["name"]) <= 64 and re.fullmatch(NAME, front["name"])
        assert 0 < len(front["description"]) <= 1024
        assert "Use when" in front["description"]
        assert "allowed-tools" not in front, "Ask before running commands instead of preapproving them."
        assert len(body.splitlines()) < 500


def test_each_skill_states_the_rules_that_keep_it_on_its_path():
    for path in _skills():
        _, body = _front_matter(path)
        assert re.search(r"^## Stay on this path\n\n- ", body, re.MULTILINE), path.parent.name


def test_skills_name_only_skills_that_exist():
    names = {path.parent.name for path in _skills()}
    for document in _plugin_documents():
        text = document.read_text(encoding="utf-8")
        named = set(re.findall(r"\b([a-z0-9]+(?:-[a-z0-9]+)+) skill\b", text))
        for group in re.findall(r"\(use ([^)]+)\)", text):
            named.update(group.split(" or "))
        assert named <= names, f"{document.name} names unknown skills: {named - names}"


def test_resource_group_commands_name_their_subscription():
    for document in _plugin_documents():
        for command in re.findall(r"\baz [^`\n]*--resource-group[^`\n]*", document.read_text(encoding="utf-8")):
            assert "--subscription" in command, f"{document.name}: {command}"


def test_install_commands_use_options_the_bootstrap_scripts_accept():
    skill = (PLUGIN / "skills" / "install-siteops" / "SKILL.md").read_text(encoding="utf-8")
    scripts = ROOT / "scripts" / "bootstrap"
    bash = (scripts / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    powershell = (scripts / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    (bash_command,) = re.findall(r"^bash siteops-bootstrap\.sh (.*)$", skill, re.MULTILINE)
    (powershell_command,) = re.findall(r"^ +(-Release .*)$", skill, re.MULTILINE)
    alternatives = re.findall(r"`(--[a-z-]+)` or `(-[A-Z][A-Za-z]+)`", skill)
    assert alternatives
    for option in re.findall(r"--[a-z-]+", bash_command) + [option for option, _ in alternatives]:
        assert re.search(rf"^\s*{option}\)", bash, re.MULTILINE), option
    for option in re.findall(r"-[A-Z][A-Za-z]+", powershell_command) + [option for _, option in alternatives]:
        assert re.search(rf"\[(?:string|switch)\]\${option[1:]}\b", powershell), option


def test_upgrade_skill_maps_each_aio_release_to_its_minor_version():
    skill = " ".join((PLUGIN / "skills" / "upgrade-aio" / "SKILL.md").read_text(encoding="utf-8").split())
    minors = {}
    for path in sorted((ROOT / "workspaces" / "iot-operations" / "parameters" / "aio-releases").glob("*.yaml")):
        version = yaml.safe_load(path.read_text(encoding="utf-8"))["aioVersion"]
        minors.setdefault(".".join(version.split(".")[:2]), []).append(path.stem)
    for minor, releases in minors.items():
        if len(releases) == 1:
            expected = f"{releases[0]} is {minor}"
        elif len(releases) == 2:
            expected = f"{releases[0]} and {releases[1]} are {minor}"
        else:
            expected = f"{releases[0]} to {releases[-1]} are {minor}"
        assert expected in skill


def test_deploy_skill_states_the_workspace_defaults():
    workspace = ROOT / "workspaces" / "iot-operations"
    skill = (PLUGIN / "skills" / "deploy-aio" / "SKILL.md").read_text(encoding="utf-8")
    contract = yaml.safe_load((workspace / "manifests" / "aio-install" / "inputs.yaml").read_text(encoding="utf-8"))
    defaults = {entry["name"]: entry.get("default") for entry in contract["inputs"]}
    assert f"AIO release {defaults['aioRelease']}" in skill
    assert f"broker memory profile `{defaults['brokerMemoryProfile']}`" in skill
    assert defaults["enableCertManager"] is True and "cert-manager enabled" in skill
    assert defaults["enableSecretSync"] is False and "Secret Sync disabled" in skill
    secretsync = yaml.safe_load((workspace / "manifests" / "secretsync" / "inputs.yaml").read_text(encoding="utf-8"))
    (instance,) = [entry for entry in secretsync["inputs"] if entry["name"] == "instance"]
    releases = sorted(
        path.stem for path in (workspace / "parameters" / "aio-releases").glob("*.yaml")
        if yaml.safe_load(path.read_text(encoding="utf-8"))["aioApiVersion"] == instance["resource"]["apiVersion"]
    )
    assert f"AIO {', '.join(releases[:-1])} or {releases[-1]} instance" in skill


def test_readme_lists_every_skill_and_the_marketplace_install():
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    for path in _skills():
        assert f"`{path.parent.name}`" in readme
    assert f"copilot plugin install {manifest['name']}@{marketplace['name']}" in readme


@pytest.mark.parametrize("document", _plugin_documents(), ids=lambda path: path.relative_to(PLUGIN).as_posix())
def test_plugin_links_resolve(document):
    for link in _links(document):
        if link.startswith(REPOSITORY_URL):
            match = re.fullmatch(
                re.escape(REPOSITORY_URL) + r"(?:/(?:blob|tree)/main/([^#]+))?(?:#(.+))?", link,
            )
            assert match, f"Link to a file on main: {link}"
            target, anchor = ROOT / (match.group(1) or "README.md"), match.group(2)
        elif re.match(r"[a-z]+:", link):
            continue
        else:
            path, _, anchor = link.partition("#")
            target = (document.parent / path).resolve()
            if document.is_relative_to(PLUGIN / "skills"):
                skill = document.relative_to(PLUGIN / "skills").parts[0]
                assert target.is_relative_to(PLUGIN / "skills" / skill), f"{link} leaves its skill"
        assert target.exists(), f"{document.name} links to a missing path: {link}"
        if anchor:
            assert anchor in _anchors(target), f"{document.name} links to a missing heading: {link}"


def test_skills_contain_commands_for_the_cli_checks():
    assert len(_siteops_commands()) >= 20


@pytest.mark.parametrize("command", _siteops_commands())
def test_skill_commands_parse_with_the_current_cli(monkeypatch, command):
    class Parsed(Exception):
        pass

    def stop_after_parsing(verbose):
        raise Parsed

    monkeypatch.setattr(sys, "argv", _argv(command))
    monkeypatch.setattr(cli, "setup_logging", stop_after_parsing)
    with pytest.raises((Parsed, SystemExit)) as stopped:
        cli.main()
    # --version prints and exits successfully during parsing. A usage error exits with 2.
    if stopped.type is SystemExit:
        assert stopped.value.code == 0


def _without(command, verb):
    """Drop the command verb, keeping global options and arguments in order."""
    position = command.index(verb)
    return command[:position] + command[position + 1:]


@pytest.mark.parametrize(
    "document", [path for skill in _skills() for path in _skill_documents(skill)],
    ids=lambda path: path.relative_to(PLUGIN).as_posix(),
)
def test_each_deploy_repeats_a_plan_shown_and_confirmed_before_it(document):
    text = document.read_text(encoding="utf-8")
    commands = [
        (match.start(), _argv(match.group(1)))
        for match in re.finditer(r"(?:^[ \t]*|`)(siteops [^`\n]+)", text, re.MULTILINE)
    ]
    deploys = False
    for position, command in commands:
        assert ("--yes" in command) is ("deploy" in command)
        if "deploy" not in command:
            continue
        deploys = True
        expected = [argument for argument in _without(command, "deploy") if argument != "--yes"]
        shown = [start for start, plan in commands if start < position and "plan" in plan and _without(plan, "plan") == expected]
        assert shown, f"{document.name} deploys without showing that plan first: {command}"
        assert "Reply deploy to run this plan." in text[shown[-1]:position], f"{document.name} deploys before asking"
    if deploys:
        skill = PLUGIN / "skills" / document.relative_to(PLUGIN / "skills").parts[0] / "SKILL.md"
        assert "Never run `plan` and `deploy` in the same turn." in skill.read_text(encoding="utf-8")


_CONSENT = {
    "skills/deploy-aio/SKILL.md": ("Reply deploy to run this plan.",),
    "skills/deploy-aio/references/fleet.md": ("Reply deploy to run this plan.",),
    "skills/upgrade-aio/SKILL.md": ("Reply deploy to run this plan.",),
    "skills/install-siteops/SKILL.md": (
        "Reply install to install Site Ops <release>.", "Reply replace to reinstall Site Ops <release>.",
    ),
}


@pytest.mark.parametrize("name", sorted(_CONSENT))
def test_consent_is_one_plain_text_reply_rather_than_a_question_form(name):
    text = " ".join((PLUGIN / name).read_text(encoding="utf-8").split())
    for reply in _CONSENT[name]:
        assert reply in text
    assert "question tool" in text
    assert "Reply yes" not in text
    if name == "skills/install-siteops/SKILL.md":
        assert text.index(_CONSENT[name][0]) < text.index('--release <release> --yes')
    readme = " ".join((PLUGIN / "README.md").read_text(encoding="utf-8").split())
    assert "Reply deploy to run this plan." in readme and "one reply is enough" in readme
