# Contributing

This project welcomes contributions and suggestions.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/Azure/digital-ops-scale-kit.git
cd digital-ops-scale-kit

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=siteops --cov-report=term-missing
```

## Code Style

- Type hints required for all functions
- Docstrings for public methods
- Follow existing patterns in the codebase

## Testing

- Add tests for new functionality
- Mock `subprocess.run` for executor tests so no real Azure calls happen
- Use fixtures from `conftest.py` for workspace setup

## Pull Request Process

1. Run `pytest` and ensure all tests pass
2. Update documentation if adding new features
3. Follow the existing code style

## Versioning

This repository uses two independent version streams with [semantic versioning](https://semver.org/):

### Scale Kit (content)

Git tags: `v1.0.0b1`, `v1.1.0`, `v2.0.0`

Covers workspace content: Bicep templates, manifests, parameter files, site examples, GitHub
workflows, and documentation. Unscoped `v*` tags are the primary release. GitHub Releases attach
to these tags and note the minimum required siteops version.

### Site Ops (tool)

Git tags: `siteops/v1.0.0b1`, `siteops/v1.1.0`

Covers the `siteops/` Python package: CLI, orchestrator, executor, models. The `siteops/v*` tag
stays in sync with the version in `siteops/__init__.py` (read dynamically by pyproject.toml).

### Guidelines

- The scale kit version cannot be more stable than siteops. If siteops is beta, the scale kit
  is beta.
- When a content release requires a new siteops version, tag both on the same commit.
- Content-only changes (new templates, manifest updates, doc fixes) bump only the `v*` tag.
- Tool-only changes (CLI features, orchestrator fixes) bump only the `siteops/v*` tag.
- Use conventional commits to distinguish change types:
  - `feat(workspace):` for new content
  - `feat(siteops):` for new tool features
  - `fix(siteops):` for tool bugfixes
  - `docs:` for documentation

### Example timeline

```text
v1.0.0b1 + siteops/v1.0.0b1    first public beta
v1.1.0b1                        add secret sync templates (siteops unchanged)
siteops/v1.0.0b2                tool bugfix (content unchanged)
v1.2.0b1                        content needing siteops fix (requires siteops >= v1.0.0b2)
v1.0.0 + siteops/v1.0.0        stable release
```

## Microsoft Open Source

Most contributions require you to agree to a Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us the rights to use your contribution. For details, visit <https://cla.opensource.microsoft.com>.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/). For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/).
