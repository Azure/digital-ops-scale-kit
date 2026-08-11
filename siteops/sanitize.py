"""Remove environment-identifying values from text the engine reports.

Redaction is a property of where output is going rather than of the text
itself. It is off for a local run, where an operator needs the full diagnostic
intact, and on in CI, whose logs and artifacts are a public surface.

A scrub keeps what makes a failure actionable, the error code, the message, and
the resource type that failed, and replaces what identifies a tenant, a
subscription, or a deployed environment.

Coverage is pattern-based and therefore partial. Resource ids, GUIDs, tokens,
Azure service hosts, and the resource group name ARM quotes in its not-found
text are covered. An
arbitrary resource name quoted in a provider message is not, because nothing
distinguishes it from ordinary prose. Treat this as reducing what reaches a
public surface rather than as a guarantee that nothing identifying does.
"""

import os
import re

# A full ARM resource id. Matched greedily to the end of the path so a trailing
# child type and name are consumed together with the parent. `:` terminates the
# match so the separator before a message survives.
_RESOURCE_ID_PATTERN = re.compile(r"/subscriptions/[^\s'\"),;:]+", re.IGNORECASE)

# A bare GUID: subscription, tenant, principal, client, or correlation id.
_GUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# A JSON Web Token, which an authentication failure can echo back in full.
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")

# Azure service domains whose leading labels are customer-chosen. Every label
# ahead of the suffix is replaced, since a private endpoint adds one
# (`account.privatelink.blob.core.windows.net`) and replacing only the first
# would redact the generic label and leave the account name. Extend this list
# rather than widening the pattern, so an in-cluster host such as
# `aio-broker.azure-iot-operations` stays readable.
_AZURE_HOST_SUFFIXES = (
    "vault.azure.net",
    "vaultcore.azure.net",
    "blob.core.windows.net",
    "dfs.core.windows.net",
    "queue.core.windows.net",
    "table.core.windows.net",
    "file.core.windows.net",
    "servicebus.windows.net",
    "azurecr.io",
    "database.windows.net",
    "azuredatalakestore.net",
    "onelake.dfs.fabric.microsoft.com",
    "fabric.microsoft.com",
)
_AZURE_HOST_PATTERN = re.compile(
    r"\b(?:[A-Za-z0-9][A-Za-z0-9-]*\.)+?(" + "|".join(re.escape(s) for s in _AZURE_HOST_SUFFIXES) + r")\b",
    re.IGNORECASE,
)

# ARM quotes a resource group rather than emitting a path in its not-found text.
# The name is bounded to the characters a resource group may contain, and the
# closing quote is not required, so an unterminated quote still redacts the name
# instead of leaking it or consuming the rest of the message.
_RESOURCE_GROUP_PATTERN = re.compile(r"(resource group ')([\w.()-]+)", re.IGNORECASE)

GUID_PLACEHOLDER = "<guid>"
RESOURCE_ID_PLACEHOLDER = "<resource-id>"
SUBSCRIPTION_PLACEHOLDER = "<subscription>"
RESOURCE_GROUP_PLACEHOLDER = "<resource-group>"
TOKEN_PLACEHOLDER = "<token>"


def _replace_resource_id(match: re.Match[str]) -> str:
    """Reduce an ARM resource id to the resource type it addresses.

    The type is what tells an operator which resource failed, and it is the
    same for every tenant. The subscription, resource group, and resource names
    around it are the identifying parts.
    """
    segments = [segment for segment in match.group(0).split("/") if segment]
    lowered = [segment.lower() for segment in segments]

    if "providers" not in lowered:
        # `/subscriptions/<id>` or `/subscriptions/<id>/resourceGroups/<name>`.
        return RESOURCE_GROUP_PLACEHOLDER if len(segments) > 2 else SUBSCRIPTION_PLACEHOLDER

    # The last `providers`, not the first. An extension resource id carries two,
    # and the second names the type that actually failed. Every AIO install and
    # upgrade failure has that shape.
    tail = segments[len(lowered) - 1 - lowered[::-1].index("providers") + 1 :]
    if not tail:
        return RESOURCE_ID_PLACEHOLDER

    # After the provider namespace the path alternates type, name, type, name.
    type_path = "/".join([tail[0]] + tail[1::2])
    return f"<{type_path}>"


def scrub(text: str | None) -> str | None:
    """Replace environment-identifying values in free text.

    Pure, so it can be tested without touching the environment. Whether to
    apply it is `scrub_for_output`'s decision, not this function's.

    Order matters. Resource ids are handled first, since one contains a
    subscription GUID that would otherwise be replaced inside a path that is
    itself about to be replaced.

    Args:
        text: The text to scrub. `None` passes through, so a caller can apply
            this to an optional error field without a guard.

    Returns:
        The scrubbed text, or `None` when `text` was `None`.
    """
    if not text:
        return text

    scrubbed = _RESOURCE_ID_PATTERN.sub(_replace_resource_id, text)
    scrubbed = _JWT_PATTERN.sub(TOKEN_PLACEHOLDER, scrubbed)
    scrubbed = _AZURE_HOST_PATTERN.sub(lambda m: f"<host>.{m.group(1)}", scrubbed)
    scrubbed = _RESOURCE_GROUP_PATTERN.sub(rf"\1{RESOURCE_GROUP_PLACEHOLDER}", scrubbed)
    return _GUID_PATTERN.sub(GUID_PLACEHOLDER, scrubbed)


# Set by a workflow to redact engine output. Also honored when explicitly set
# to a falsy value, which is how an operator debugging a self-hosted runner
# turns redaction off deliberately.
REDACT_ENV = "SITEOPS_REDACT_OUTPUT"

# Environments whose logs and artifacts are published. Redaction defaults on
# here, so a workflow added later is covered without remembering to opt in.
_CI_ENV_MARKERS = ("GITHUB_ACTIONS", "TF_BUILD")

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def is_redaction_enabled() -> bool:
    """Whether engine output is bound for a surface that must not carry identities.

    An explicit `SITEOPS_REDACT_OUTPUT` wins in both directions. Otherwise a
    recognized CI environment turns redaction on, so the default is safe rather
    than convenient.
    """
    explicit = os.environ.get(REDACT_ENV)
    if explicit is not None:
        lowered = explicit.strip().lower()
        if lowered in _TRUTHY:
            return True
        if lowered in _FALSY:
            return False

    return any(os.environ.get(marker) for marker in _CI_ENV_MARKERS)


def scrub_for_output(text: str | None) -> str | None:
    """Scrub `text` when the destination is a published surface.

    The boundary is result construction: a failure is scrubbed as it becomes a
    result, so the log line, the site result, and the step result all carry the
    same text, and so does an artifact written from a result. The calls in
    `_print_deployment_summary` are a backstop for a result built by a path that
    skips this, not the primary boundary.
    """
    return scrub(text) if is_redaction_enabled() else text
