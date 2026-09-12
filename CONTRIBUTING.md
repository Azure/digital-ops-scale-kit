# Contributing

This project welcomes contributions and suggestions.

## Development setup

Use Python 3.10 or newer. Clone the repository and create a virtual environment:

```bash
git clone https://github.com/Azure/digital-ops-scale-kit.git
cd digital-ops-scale-kit
python -m venv .venv
```

Activate it with `source .venv/bin/activate` on Linux or
`.\.venv\Scripts\Activate.ps1` in PowerShell, then install the development
dependencies:

```bash
python -m pip install -e ".[dev]"
```

Run the local validation used for ordinary changes:

```bash
pytest -m "not integration"
ruff check .
```

The editable install is a contributor workflow. Operators installing an
identified build should use the [Site Ops installation guide](docs/install-siteops.md).

## Choose the right layer

Site Ops is the generic orchestration engine under `siteops/`. Azure IoT
Operations behavior belongs under `workspaces/iot-operations/`.

- Put reusable workspace loading, targeting, preparation, execution, and
  result behavior in the engine.
- Put AIO release policy, templates, manifests, parameters, sites, and samples
  in the IoT Operations workspace.
- Keep GitHub Actions and Azure Pipelines behavior aligned when a feature is
  shared by both delivery surfaces.

See the [repository and workspace guide](docs/repository-guide.md) for the
architecture and directory responsibilities.

## Code and tests

- Add type hints to functions and docstrings to public methods.
- Follow the patterns already used in the surrounding code.
- Add focused tests for changed behavior.
- Use fixtures from `conftest.py` for workspace setup.
- Inject command runners in planning and executor tests. Guard
  `subprocess.Popen` when a real process would cross the test boundary.
- Keep live Azure tests opt-in. See [end-to-end testing](docs/e2e-testing.md)
  before running an integration scenario.

Packaging tests require the development dependencies because they build a
wheel with package-index access disabled.

Native installation tests use pipx in isolated temporary directories. On
Windows, select a short owned `pytest --basetemp` path so generated executable
paths remain within the platform limit. For offline tests, set
`SITEOPS_TEST_BACKEND_WHEELHOUSE` to a directory containing the pip wheel
pinned in `scripts/siteops-build-requirements.txt`.

## Documentation

Update the nearest operator guide when a command, field, output, prerequisite,
or side effect changes. Examples should state what must be edited before they
run and distinguish:

- compile-free validation
- executable planning without Azure or Kubernetes mutation
- deployment that creates or updates provider resources
- readiness or functional verification after deployment

Keep workspace trust, permissions, cost, private output, and cleanup
boundaries visible where they affect an operator decision.

## Pull requests

Before opening a pull request:

1. Run the smallest focused tests for the change.
2. Run `pytest -m "not integration"` and `ruff check .` when the change affects
   the engine or shared workspace behavior.
3. Update documentation and samples that exercise the changed contract.
4. Describe any live validation separately from local or hosted test results.

## Versioning

The repository has independent version streams:

| Stream | Tags | Covers |
|---|---|---|
| Scale Kit content | `v*` | Workspace content, delivery workflows, and documentation |
| Site Ops engine | `siteops/v*` | The `siteops` Python package and CLI |

Content release declarations select an exact published Site Ops version or
request an identified engine build during preview. A content release can move
without an engine release, and an engine fix can move without a content
release. The Scale Kit version cannot be more stable than the Site Ops version
it requires.

Use conventional commit scopes that identify the changed layer:

- `feat(workspace):` for new content
- `feat(siteops):` for engine features
- `fix(siteops):` for engine fixes
- `docs:` for documentation

Follow [Prepare and publish a release](docs/releasing.md) for version updates,
candidate review, and publication. Use
[Install Site Ops from a release](docs/install-siteops.md) to verify the
operator-facing installation path.

## Build local distribution artifacts

Use a clean checkout and an isolated Python 3.11 or newer build environment.
Install `scripts/siteops-build-requirements.txt` with pip's
`--require-hashes` and `--only-binary=:all:` options through your approved
package feed.

```text
python scripts/build-siteops-bundle.py
  --repository <owner/repository>
  --source-ref <full Git ref>
  --expected-source-sha <full source commit>
  --build-number <positive integer>
  --build-attempt <positive integer>
  --output <absolute output directory>/siteops-install.zip
  --download-dependencies
```

Join the lines for your shell. `--download-dependencies` permits hash-pinned
runtime wheel downloads from the configured feed. Use
`--wheelhouse <directory>` instead when you already have the complete locked
wheel set. Exactly one of these options is required.

The output directory receives `siteops-install.zip` and the standalone Site
Ops wheel. Both contain identical application wheel bytes, and existing
artifacts are not overwritten. `--version-mode build` is the default and adds
the build identity suffix. `--version-mode source` retains the source package
version for an independently versioned engine release.

Local generation creates no GitHub attestations and is not an official
release. When runtime dependencies change, update the runtime lock, target
wheel coverage, and notices together. Build tooling is pinned separately and
is not redistributed in the archive. The runtime lock accepts unconditional
exact dependencies with a compatible wheel for every declared target.

## Microsoft Open Source

Most contributions require a Contributor License Agreement (CLA) confirming
that you have the right to grant us permission to use your contribution. See
<https://cla.opensource.microsoft.com>.

This project follows the
[Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
See the
[Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/)
for more information.
