"""Compatibility imports for the explicit, runtime-owned ExperimentSession.

All state belongs to the facade. These functions neither attach attributes to
runtime objects nor mutate their configuration.
"""
from .extensions.materials import PREFIX_CHUNK_BYTES, INLINE_PREFIX_BYTES
from .extensions.session import ExperimentSession, ExperimentRuntimePort


def capture_initial_manifest(runtime):
    return runtime.experiments.capture_initial_manifest()


def raw_state(runtime):
    return runtime.experiments.raw_state()


def dispatch(runtime, method, **kwargs):
    return runtime.experiments.dispatch(method, **kwargs)
