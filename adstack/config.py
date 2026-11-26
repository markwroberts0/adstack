"""Load an Adstack configuration: one YAML file, or a directory of YAML files.

Like a Terraform module, every ``*.yaml`` and ``*.yml`` file in a directory belongs
to one configuration. Files are merged: lists are concatenated (``campaigns``),
mappings are combined (``ad_templates`` may be split across files), and defining
the same key twice is an error. Each file must declare ``version: 1``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .providers import get_provider

FORMAT_VERSION = 1
SUFFIXES = (".yaml", ".yml")
MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class Config:
    """A validated configuration."""

    provider: str
    """Provider name, for example ``google_ads``."""
    desired: dict[str, Any]
    """Provider-normalized desired state. JSON-serializable and deterministic."""
    sources: dict[str, str]
    """Original file contents by file name. Saved plans embed them."""

    def summary(self) -> str:
        return get_provider(self.provider).summary(self.desired)


class _StrictLoader(yaml.SafeLoader):
    """A SafeLoader that rejects duplicate and non-string mapping keys."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> dict[str, Any]:
    loader.flatten_mapping(node)
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        line = key_node.start_mark.line + 1
        if not isinstance(key, str):
            raise ConfigError(f"line {line}: mapping keys must be strings, found {key!r}")
        if key in result:
            raise ConfigError(f"line {line}: duplicate key {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def read_sources(path: str | os.PathLike[str]) -> dict[str, str]:
    """Read a configuration file, or every YAML file directly inside a directory."""
    path = Path(path)
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.is_file() and p.suffix in SUFFIXES and not p.name.startswith("."))
        if not files:
            raise ConfigError(f"{path}: no configuration files (*.yaml or *.yml) found")
    elif path.is_file():
        files = [path]
    else:
        raise ConfigError(f"{path}: no such file or directory")
    sources = {}
    for file in files:
        if file.stat().st_size > MAX_FILE_BYTES:
            raise ConfigError(f"{file.name}: larger than {MAX_FILE_BYTES:,} bytes")
        try:
            sources[file.name] = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ConfigError(f"{file.name}: not valid UTF-8 text") from None
    return sources


def load(path: str | os.PathLike[str]) -> Config:
    """Load and validate the configuration at ``path`` (a file or a directory)."""
    return load_sources(read_sources(path))


def load_sources(sources: dict[str, str]) -> Config:
    """Validate configuration text, given as ``{file name: YAML text}``."""
    if not sources:
        raise ConfigError("no configuration files")
    document: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for name in sorted(sources):
        _merge(document, origins, _parse(name, sources[name]), name)
    providers = document.pop("provider", None)
    if not isinstance(providers, dict) or len(providers) != 1:
        raise ConfigError("provider: configure exactly one provider, for example 'provider: {google_ads: {...}}'")
    ((name, settings),) = providers.items()
    provider = get_provider(name)
    desired = provider.load(settings, document)
    return Config(provider=name, desired=desired, sources=dict(sources))


def _parse(name: str, text: str) -> dict[str, Any]:
    try:
        document = yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{name}: invalid YAML: {exc}") from None
    except ConfigError as exc:
        raise ConfigError(f"{name}: {exc}") from None
    if not isinstance(document, dict):
        raise ConfigError(f"{name}: expected a mapping that starts with 'version: {FORMAT_VERSION}'")
    version = document.pop("version", None)
    if type(version) is not int or version != FORMAT_VERSION:
        raise ConfigError(f"{name}: 'version: {FORMAT_VERSION}' is required (the configuration format version)")
    return document


def _merge(document: dict[str, Any], origins: dict[str, str], fragment: dict[str, Any], name: str) -> None:
    for key, value in fragment.items():
        if key not in document:
            document[key] = value
            origins[key] = name
            if isinstance(value, dict):
                for sub in value:
                    origins[f"{key}.{sub}"] = name
            continue
        current = document[key]
        if isinstance(current, list) and isinstance(value, list):
            document[key] = current + value
        elif isinstance(current, dict) and isinstance(value, dict):
            for sub, item in value.items():
                where = f"{key}.{sub}"
                if sub in current:
                    raise ConfigError(f"{where} is defined in both {origins[where]} and {name}")
                current[sub] = item
                origins[where] = name
        else:
            raise ConfigError(f"{key} is defined in both {origins[key]} and {name}")
