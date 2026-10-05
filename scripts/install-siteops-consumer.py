# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Install one identified engine for a Linux pipeline through the verified bootstrap."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fleet_process import FleetProcessError  # noqa: E402
from fleet_process import run as run_private  # noqa: E402


class ConsumerSetupError(ValueError):
    def __init__(self, message: str, code: int = 1):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Selection:
    repository: str
    release: str
    commit: str
    inferred: bool


def select(environment: dict[str, str]) -> Selection:
    release = environment.get("SITEOPS_RELEASE", "").strip()
    commit = environment.get("SITEOPS_SOURCE_COMMIT", "").strip()
    repository = environment.get("SITEOPS_REPOSITORY", "").strip()
    inferred = not any((release, commit, repository))
    if inferred:
        ref = environment.get("SITEOPS_AUTOMATION_REF", "")
        if environment.get("SITEOPS_AUTOMATION_PROVIDER", "").casefold() != "github" or not ref.startswith("refs/tags/"):
            raise ConsumerSetupError(
                "Select a tagged GitHub template repository, an explicit release and sourceCommit, "
                "or siteopsSource. An unpinned checkout is not an engine selection."
            )
        release = ref.removeprefix("refs/tags/")
        commit = environment.get("SITEOPS_AUTOMATION_VERSION", "")
        repository = environment.get("SITEOPS_AUTOMATION_REPOSITORY", "")
    else:
        repository = repository or "Azure/digital-ops-scale-kit"
    if (
        re.fullmatch(r"(siteops/)?v[0-9][0-9A-Za-z._-]{0,100}", release) is None
        or re.fullmatch("[0-9a-f]{40}", commit) is None
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
    ):
        raise ConsumerSetupError("Release installation requires an exact release, sourceCommit and GitHub repository.")
    return Selection(repository, release, commit, inferred)


def install(
    selection: Selection, source: Path, parent: Path, *, environment: dict[str, str],
    runner=run_private,
) -> Path:
    if os.name != "posix" or not source.is_absolute() or not parent.is_absolute():
        raise ConsumerSetupError("Verified pipeline installation requires Linux and absolute tooling and state directories.")
    script = source / "scripts" / "bootstrap" / "siteops-bootstrap.sh"
    if not script.is_file() or script.is_symlink():
        raise ConsumerSetupError("The selected automation checkout does not contain the reviewed bootstrap.")
    state = Path(tempfile.mkdtemp(prefix="siteops-consumer-", dir=parent))
    for name in ("home", "data", "tools", "bin", "python", "github", "azure", "logs"):
        (state / name).mkdir(mode=0o700)
    child = {
        key: value for key, value in environment.items()
        if key in {
            "PATH", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SITEOPS_REDACT_OUTPUT", "TF_BUILD",
        } or key.startswith("UV_")
    }
    child.update({
        "HOME": str(state / "home"), "XDG_DATA_HOME": str(state / "data"),
        "UV_TOOL_DIR": str(state / "tools"), "UV_TOOL_BIN_DIR": str(state / "bin"),
        "UV_PYTHON_INSTALL_DIR": str(state / "python"), "GH_CONFIG_DIR": str(state / "github"),
        "AZURE_CONFIG_DIR": str(state / "azure"),
    })

    def execute(name: str, arguments: list[str], timeout: int) -> bytes:
        code = runner(arguments, cwd=source, logs=state / "logs", name=name, timeout=timeout, environment=child)
        if code:
            raise ConsumerSetupError(
                f"Site Ops {name} failed. Private installation diagnostics remain in the agent temporary directory.",
                code,
            )
        with (state / "logs" / f"{name}.out").open("rb") as stream:
            return stream.read(4097)

    if selection.inferred:
        actual = execute("source-check", ["git", "-C", str(source), "rev-parse", "HEAD"], 30).decode().strip()
        if actual != selection.commit:
            raise ConsumerSetupError("The automation checkout differs from the selected repository resource version.")
    execute("bootstrap", [
        "bash", str(script),
        "--release" if selection.release.startswith("siteops/") else "--content-release", selection.release,
        "--source-commit", selection.commit, "--repository", selection.repository, "--yes",
    ], 1200)
    command = state / "bin" / "siteops"
    version = execute("version", [str(command), "--version"], 30).decode("utf-8").strip()
    if re.fullmatch(r"siteops [A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}", version) is None:
        raise ConsumerSetupError("The installed Site Ops command returned an invalid version.")
    print(version)
    return command.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--state-parent", type=Path, required=True)
    args = parser.parse_args()
    try:
        selected = select(dict(os.environ))
        command_directory = install(
            selected, args.source_directory, args.state_parent, environment=dict(os.environ),
        )
    except (ConsumerSetupError, FleetProcessError) as error:
        message = str(error) if isinstance(error, ConsumerSetupError) else "Installer process shutdown was not confirmed."
        print(f"Site Ops setup: {message}", file=sys.stderr)
        return error.code
    except (OSError, UnicodeError, ValueError):
        print("Site Ops setup could not access valid source, state or command output.", file=sys.stderr)
        return 1
    data = str(command_directory).replace("%", "%AZP25").replace("\r", "%0D").replace("\n", "%0A")
    print("##vso[task.prependpath]" + data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
