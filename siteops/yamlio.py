"""Load the YAML the engine reads, rejecting a mapping key written twice.

Sites, manifests, and parameter files are all operator-authored YAML, and YAML
keeps the last of a repeated key. A parameter file that declares `dataflows:`
in two places therefore deploys only the second block, and a site that sets
`properties:` twice loses the first. Nothing downstream can detect that, since
the discarded content is gone by the time the engine sees the document.

Every engine YAML read goes through `load`, so the guard covers each surface
once rather than per caller. `DuplicateKeyError` derives from `yaml.YAMLError`,
so a caller that already tolerates a parse error keeps its behavior.
"""

import collections.abc

import yaml


class DuplicateKeyError(yaml.constructor.ConstructorError):
    """A mapping key appeared twice in one document."""


class _StrictLoader(yaml.SafeLoader):
    """`SafeLoader` that fails on a duplicate mapping key rather than dropping one."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False):
    # A generator, matching the constructor it replaces. PyYAML yields the
    # empty mapping first so an anchor can refer to it before it is filled,
    # which is what lets a recursive structure load, and defers the rest
    # through its own state machine rather than the Python stack.
    data: dict = {}
    yield data

    seen: set = set()
    for key_node, _ in node.value:
        # Both are rewritten by the parent constructor after this pre-pass:
        # `<<` is flattened into the mapping and `=` becomes a string. Neither
        # is a key to compare here, and constructing them now would fail.
        if key_node.tag in ("tag:yaml.org,2002:merge", "tag:yaml.org,2002:value"):
            continue
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, collections.abc.Hashable):
            # The predicate the standard constructor uses, so the two cannot
            # disagree. It reports the unhashable key with its own message.
            continue
        if key in seen:
            raise DuplicateKeyError(
                "while constructing a mapping",
                node.start_mark,
                (
                    f"found a duplicate key {key!r}. YAML keeps the last one, so "
                    f"the first is discarded. Merge them or rename one."
                ),
                key_node.start_mark,
            )
        seen.add(key)

    data.update(yaml.SafeLoader.construct_mapping(loader, node, deep=deep))


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def load(stream):
    """Parse one YAML document from `stream`, which may be text or a file object.

    Returns:
        The parsed document, or `None` for an empty one, matching
        `yaml.safe_load`.

    Raises:
        DuplicateKeyError: A mapping key appeared twice. The message names the
            key and both positions.
        yaml.YAMLError: The document is not well-formed.
    """
    return yaml.load(stream, Loader=_StrictLoader)
