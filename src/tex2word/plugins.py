"""Small extension API for project-local tex2word Python plugins.

Plugins are intentionally narrow: they may rewrite the flattened LaTeX source
before the normal parser runs, and they may add pylatexenc macro/environment
signatures so custom commands consume their arguments correctly.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Protocol

from pylatexenc.macrospec import EnvironmentSpec, MacroSpec

from .report import ConversionReport

SourcePreprocessor = Callable[[str, str, ConversionReport], str]


class RegisterFunction(Protocol):
    def __call__(self, registry: PluginRegistry) -> object: ...


PluginRef = str | ModuleType | RegisterFunction
PluginRefs = PluginRef | Iterable[PluginRef]


@dataclass
class PluginRegistry:
    """Mutable registry passed to a plugin's ``register(registry)`` function."""

    source_preprocessors: list[SourcePreprocessor] = field(default_factory=list)
    macro_specs: list[MacroSpec] = field(default_factory=list)
    environment_specs: list[EnvironmentSpec] = field(default_factory=list)

    def add_preprocessor(self, fn: SourcePreprocessor) -> None:
        self.source_preprocessors.append(fn)

    def add_macro(self, name: str | MacroSpec, argspec: str = "") -> None:
        self.macro_specs.append(name if isinstance(name, MacroSpec) else MacroSpec(name, argspec))

    def add_environment(self, name: str | EnvironmentSpec, argspec: str = "") -> None:
        self.environment_specs.append(
            name if isinstance(name, EnvironmentSpec) else EnvironmentSpec(name, argspec)
        )


def load_plugins(
    plugins: PluginRefs | None,
    *,
    base_dir: str = ".",
) -> PluginRegistry:
    """Load user-requested plugins from module names, ``.py`` paths, or callables."""

    registry = PluginRegistry()
    for ref in _iter_plugin_refs(plugins):
        if isinstance(ref, ModuleType):
            _register_module(ref, registry)
        elif isinstance(ref, str):
            _register_module(_import_plugin(ref, base_dir), registry)
        elif callable(ref):
            ref(registry)
        else:
            raise TypeError(f"unsupported tex2word plugin reference: {ref!r}")
    return registry


def _iter_plugin_refs(plugins: PluginRefs | None) -> Iterable[PluginRef]:
    if plugins is None:
        return ()
    if isinstance(plugins, str) or isinstance(plugins, ModuleType) or callable(plugins):
        return (plugins,)
    return plugins


def _register_module(module: ModuleType, registry: PluginRegistry) -> None:
    for name in ("register", "setup", "tex2word_plugin"):
        fn = getattr(module, name, None)
        if callable(fn):
            fn(registry)
            return
    preprocess = getattr(module, "preprocess_source", None)
    if callable(preprocess):
        registry.add_preprocessor(preprocess)
        return
    raise TypeError(
        f"tex2word plugin {module.__name__!r} must define register(registry) "
        "or preprocess_source(source, base_dir, report)"
    )


def _import_plugin(ref: str, base_dir: str) -> ModuleType:
    if _looks_like_path(ref):
        path = _resolve_plugin_path(ref, base_dir)
        module_name = "tex2word_user_plugin_" + str(abs(hash(os.path.abspath(path))))
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import tex2word plugin from {path!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return importlib.import_module(ref)


def _looks_like_path(ref: str) -> bool:
    return ref.endswith(".py") or "/" in ref or "\\" in ref


def _resolve_plugin_path(ref: str, base_dir: str) -> str:
    candidates = [ref] if os.path.isabs(ref) else [ref, os.path.join(base_dir, ref)]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    raise FileNotFoundError(f"tex2word plugin file not found: {ref}")
