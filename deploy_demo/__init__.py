"""Shared, dependency-free contracts for the HomeOps Fleet Deploy Lab."""

from .deployment_spec import (
    COLORS,
    ENVIRONMENTS,
    FAILURE_MODES,
    IMPLEMENTATIONS,
    SCHEMA_VERSION,
    SHAPES,
    STRATEGIES,
    TARGETS_BY_ENVIRONMENT,
    DeploymentSpec,
    DeploymentSpecError,
    canonicalize_deployment_spec,
    expand_targets,
    validate_deployment_spec,
)

__all__ = [
    "COLORS",
    "ENVIRONMENTS",
    "FAILURE_MODES",
    "IMPLEMENTATIONS",
    "SCHEMA_VERSION",
    "SHAPES",
    "STRATEGIES",
    "TARGETS_BY_ENVIRONMENT",
    "DeploymentSpec",
    "DeploymentSpecError",
    "canonicalize_deployment_spec",
    "expand_targets",
    "validate_deployment_spec",
]
