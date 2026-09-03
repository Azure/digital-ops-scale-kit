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

# A user principal name or email address. Azure echoes one back in a
# `lastModifiedBy` field and in several permission errors, and it identifies a
# person rather than a resource. The local part is replaced and the domain is
# kept, since the domain is what makes the message diagnostic.
#
# `#` is part of the local part because that is the Azure AD guest form,
# `alice_fabrikam.com#EXT#@contoso.onmicrosoft.com`, which is exactly what a
# `lastModifiedBy` carries for a guest.
#
# The lookbehind covers every character the local part can contain, plus `/`,
# which keeps a URL authority out. Blocking `/` alone stops a match only at the
# first character, so `abfss://my-ws@account.dfs.core.windows.net` would match
# from `ws` and be rewritten into an address that no longer resolves. A host is
# left to the host rule.
_UPN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+\-/])[A-Za-z0-9._%+#-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
)

GUID_PLACEHOLDER = "<guid>"
RESOURCE_ID_PLACEHOLDER = "<resource-id>"
SUBSCRIPTION_PLACEHOLDER = "<subscription>"
RESOURCE_GROUP_PLACEHOLDER = "<resource-group>"
KUBECONFIG_PLACEHOLDER = "<kubeconfig>"
UPN_PLACEHOLDER = "<user>"
TOKEN_PLACEHOLDER = "<token>"
SITE_PLACEHOLDER = "<site>"


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
    itself about to be replaced. A user principal name is handled before the
    host rule, so an address at an Azure service domain has its local part
    removed rather than only its host labels.

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
    scrubbed = _UPN_PATTERN.sub(rf"{UPN_PLACEHOLDER}@\1", scrubbed)
    scrubbed = _AZURE_HOST_PATTERN.sub(lambda m: f"<host>.{m.group(1)}", scrubbed)
    scrubbed = _RESOURCE_GROUP_PATTERN.sub(rf"\1{RESOURCE_GROUP_PLACEHOLDER}", scrubbed)
    return _GUID_PATTERN.sub(GUID_PLACEHOLDER, scrubbed)


# Flags whose value names the environment rather than the operation. A
# subscription is included because the CLI accepts a display name as well as a
# GUID, and a display name is not covered by the GUID rule.
_IDENTIFYING_FLAGS = {
    "-g": RESOURCE_GROUP_PLACEHOLDER,
    "--resource-group": RESOURCE_GROUP_PLACEHOLDER,
    "--subscription": SUBSCRIPTION_PLACEHOLDER,
    # A per-proxy kubeconfig lands in the OS temp directory, whose path carries
    # the account name on Windows. The path names the machine, not the command.
    "--kubeconfig": KUBECONFIG_PLACEHOLDER,
}


def _render_flag(token: str) -> tuple[str, str | None]:
    """Render one argument, and say what a following value should become.

    The single place a token is classified. Returning the pending placeholder
    rather than setting it lets the caller apply the same rules to a token in
    any position, which is what stops a flag that follows a valueless flag from
    being treated as a bare word.

    Returns:
        The token as it should appear, and the placeholder that replaces the
        next token when this one is a flag that takes its value separately.
    """
    placeholder = _IDENTIFYING_FLAGS.get(token)
    if placeholder is not None:
        return token, placeholder

    flag, separator, _ = token.partition("=")
    if separator and flag in _IDENTIFYING_FLAGS:
        return f"{flag}={_IDENTIFYING_FLAGS[flag]}", None

    return token, None


def scrub_command(argv: list[str]) -> list[str]:
    """Replace identifying flag values in an argument vector.

    Works on the vector rather than on the rendered string, which is what makes
    it exact. A value may contain a space, since a subscription display name
    can, and once the vector is joined that space is indistinguishable from the
    separator, so the tail of the value survives any pattern. Matching on whole
    tokens also means a flag is a flag: a value ending in `-g` is not one, and
    a flag left without a value cannot consume the flag that follows it.

    Args:
        argv: The argument vector, as passed to `subprocess`.

    Returns:
        A new vector with identifying values replaced. Values are replaced
        whether written as two tokens or as `--flag=value`.
    """
    scrubbed: list[str] = []
    replace_next: str | None = None

    for token in argv:
        # A flag expecting a value, followed by another flag, means the value
        # is missing. The following token is classified from scratch rather
        # than consumed, so it keeps whatever treatment it deserves in its own
        # right.
        if replace_next is not None and not token.startswith("-"):
            scrubbed.append(replace_next)
            replace_next = None
            continue

        rendered, replace_next = _render_flag(token)
        scrubbed.append(rendered)

    return scrubbed


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


def report_parameter_selection_error(error: Exception) -> str:
    """Hide selected values, site names, and paths on published surfaces."""
    if is_redaction_enabled():
        return (
            "Parameter file selection failed. Re-run locally with output "
            "redaction disabled for site and path details."
        )
    return str(error)


def scrub_for_output(text: str | None) -> str | None:
    """Scrub `text` when the destination is a published surface.

    The boundary is result construction: a failure is scrubbed as it becomes a
    result, so the log line, the site result, and the step result all carry the
    same text, and so does an artifact written from a result. The calls in
    `_print_deployment_summary` are a backstop for a result built by a path that
    skips this, not the primary boundary.
    """
    return scrub(text) if is_redaction_enabled() else text


def site_name_for_output(site_name: str) -> str:
    """Return a site label suitable for the current output destination."""
    return SITE_PLACEHOLDER if is_redaction_enabled() else site_name


def scrub_site_for_output(text: str | None, site_name: str) -> str | None:
    """Scrub ordinary identifiers plus one known site name."""
    if not is_redaction_enabled():
        return text
    scrubbed = scrub(text)
    if not scrubbed or not site_name:
        return scrubbed
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(site_name)}(?![A-Za-z0-9_])"
    )
    return pattern.sub(SITE_PLACEHOLDER, scrubbed)


def report_site_load_error(error: Exception) -> str:
    """Keep site names and trusted local paths out of published diagnostics."""
    if is_redaction_enabled():
        return (
            "Site configuration could not be loaded. Re-run locally with "
            "output redaction disabled for site and path details."
        )
    return str(error)


def scrub_command_for_output(argv: list[str]) -> str:
    """Render an argument vector for a log, scrubbed when output is published.

    Pairs `scrub_command` with `scrub`, so a value named by a flag is replaced
    structurally while everything else in the line, such as a resource id
    inside a path, still goes through the text rules.
    """
    if not is_redaction_enabled():
        return " ".join(argv)
    return scrub(" ".join(scrub_command(argv))) or ""
