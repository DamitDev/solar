"""Filesystem naming helpers shared across modules."""


def sanitize_alias_for_fs(alias: str) -> str:
    """Make *alias* safe to use as a filesystem path component.

    Solar aliases are ``name:tag`` by convention and may carry a slash
    (e.g. a merged ``owner/repo:tag`` name), both of which are illegal or
    ambiguous in directory names.
    """
    return alias.replace(":", "-").replace("/", "-")
