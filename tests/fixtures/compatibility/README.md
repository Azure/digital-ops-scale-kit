# Released consumer contracts

Each directory holds what a release gave its users, copied from its tag or
written by its engine. Tests check that the current templates and engine
still accept it. Keep these files unchanged. To protect a later release, add
a directory named for its tag, and name that release in each place listed
under [Protect another release](#protect-another-release).

- `ado-consumer` is `tests/fixtures/ado-consumer` at the tag: a caller
  workspace, its Site and a standalone Site file.
- `ado-templates.json` records the name, type and default of every parameter
  of the supported templates at the tag: `siteops-validate.yaml`,
  `siteops-deploy.yaml` and `setup-siteops.yaml`.
- `saved-data` holds files that the engine at the tag wrote: a workspace pin,
  an approved source, cache entries including a published package with its
  extracted content, an answer file, a saved Site and a content index. It
  also holds the authored inputs for those commands: the input contract
  `inputs/smoke.inputs.yaml`, the answer file with its values filled in
  `inputs/answers-filled.yaml`, and the workspace sources under `index/`.
  `docs/saved-data.md` lists every file Site Ops saves.

`tests/test_consumer_compatibility.py` checks the caller fixture and template
parameters. The current templates must keep each recorded parameter with the
same type, and the same default when it has one. A parameter added later
needs a default, so a caller that passes only the recorded parameters still
expands.

`tests/test_saved_data_compatibility.py` reads the saved files with the
current engine and checks that it writes the same bytes for the same content.

Git keeps these files byte for byte, without line ending conversion, because
several of them are pinned by their SHA-256.

## Protect another release

Only the **Pip Consumer** job in `.github/workflows/ci.yaml` finds every
`compatibility/*/ado-consumer` directory by itself. These places name
`v1.0.0b7` and need the new release added beside it:

- `tests/test_consumer_compatibility.py`: `RELEASE` and `RELEASE_COMMIT`,
  the tag and the commit it must resolve to.
- `tests/test_saved_data_compatibility.py`: `RELEASE`, the fixture
  directory.
- `tests/test_ado_consumer_smoke.py`: `RELEASED` and `STAGES`.
- `.github/workflows/ci.yaml` and `.pipelines/ci.yaml`: the unit test step
  that fetches the tag.
- `.pipelines/templates/consumer-smoke.yaml`: the `validate_b7_selector`
  and `validate_b7_site_file` stages for the frozen caller.
- `.pipelines/validate-pipelines.yaml`: those stages in the
  `qualification_report` stage's `dependsOn` list, and their
  `B7_SELECTOR_RESULT` and `B7_SITE_FILE_RESULT` values in the report job's
  variables and the summary step's `env`.
- `scripts/summarize-ado-qualification.py`: the stage names and result
  variables, which `tests/test_ado_qualification_report.py` also lists.
