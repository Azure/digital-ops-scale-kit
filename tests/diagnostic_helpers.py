"""Expected public diagnostic text comes from its registered definition."""

from siteops.diagnostics import REGISTRY, public_entry, resolve_member


def published(code, severity="error", *, source_code=None):
    kind = "DeploymentRun" if code.startswith("run.") else "DeploymentPlan"
    member = resolve_member(source_code, kind) if source_code else REGISTRY[code].member
    entry = public_entry(member, None)
    return {
        "code": code, "severity": severity, "summary": entry.summary,
        "guidance": entry.guidance, "documentation": entry.documentation,
    }
