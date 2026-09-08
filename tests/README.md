# tests/

Tests are split by what each layer needs to run. Unit and workspace coverage
run on every change. A test placed in the wrong layer either slows that lane
or misses the environment it is meant to exercise.

| Layer | Location | Needs | Runs |
|---|---|---|---|
| **Unit** | `tests/*.py` | Synthetic temporary files, with external tools injected or forbidden | Every change |
| **Workspace** | `tests/workspace/` | The committed workspace, Azure CLI and Bicep, kubectl for executable capability preflight, and Bash for delivery-script checks | Every change |
| **Integration** | `tests/integration/` | A live Azure subscription and an Arc-connected cluster | On request, and inside the E2E workflow |
| **E2E fixtures** | `tests/e2e/` | Rendered at runtime by the workflow | Not collected by pytest |

```bash
pytest tests/ -m "not integration"     # the per-change lane, what CI runs
pytest tests/workspace -q              # workspace contracts only
```

Delivery-script checks use native Bash on Linux and macOS, and Git Bash from
Git for Windows on Windows. The Windows WSL launcher is not a substitute for
an installed shell. These checks run fake deployment commands and require no
Azure authentication.

## Choosing a layer

**Unit** covers engine behavior without depending on committed workspace
content or installed deployment tools: parsing, selector grammar, the merge
order, polling and retry classification, and redaction. Build inputs under the
test's temporary directory and inject local-tool behavior so the test does not
change meaning with the host or workspace.

**Workspace** covers the committed content itself: that manifests validate, that
chaining references resolve to outputs a producing step emits, that declarations
match the contract their templates expect, and that the shipped Bicep compiles.
These read `workspaces/iot-operations/` through the `workspace` fixture in
`tests/workspace/conftest.py`.

**Integration** covers what only a real deployment shows: that ARM accepted the
template, that a resource projected to the cluster, and that a redeploy
reconciles rather than recreates. Every module here carries
`pytestmark = [pytest.mark.integration]`, and markers are registered strictly,
so a typo in that name fails collection rather than silently dropping the file.

## Writing a guard that cannot pass vacuously

Every one of these was found by injecting a defect rather than by review, and
each has caught a real one since.

- **Count the things you check, not the files you found them in.** A sweep that
  asserts `assert files` and then iterates entries passes when every file
  yields zero entries.
- **Assert discovery reaches each location it sweeps.** A glob that stopped
  matching leaves every test keyed on it green while checking a fraction of the
  workspace.
- **Prove a new test fails before trusting it.** Inject the exact defect it
  claims to catch, watch it fail, then revert.
- **Prefer an allowlist to a denylist when filtering diagnostics.** A denylist
  admits whatever a tool adds later.
- **A warning can mean the tool stopped checking.** Treat it as fatal, or a test
  depending on that check reports success having checked nothing.
- **Check that the composed entry point is what runs.** A step pointing at one
  component of a composition succeeds while doing a fraction of the work.

## Fixtures that are shared, and why they restore

`Orchestrator.load_site` caches, so every test in a module receives the same
`Site` object. A test that mutates one restores it afterwards, otherwise it
changes what the next test resolves and the failure depends on execution order.
`tests/workspace/test_catalog_gating.py` and `tests/integration/conftest.py`
both do this.
