"""Shared contracts — types/vocab/errors/authz agreed across packages and serialized to
clients. No behavior, no I/O; depends on nothing. See docs/access/entity_api_model.md §4.

Holds so far the authorization contracts and the error taxonomy (the foundation the
access-API gateway is built on). Cross-package DTOs and the open vocabularies migrate here
in Phase 3 as the entity-API engine consumes them.
"""
from .errors import (
    CatalogueError,
    Conflict,
    IntegrityViolation,
    NotFound,
    StaleWrite,
    ValidationError,
)
from .authz import (
    SYSTEM,
    AccessMode,
    Action,
    AllowAll,
    Denied,
    Policy,
    Principal,
)
from .refs import Ref
from .dto import (
    Collection,
    Edition,
    Holding,
    Person,
    Subject,
    Tradition,
    WishlistItem,
    Work,
    edition_fingerprint,
    person_fingerprint,
    wishlist_fingerprint,
    work_fingerprint,
)
from .impact import (
    Block,
    FileOp,
    Impact,
    LinkRepoint,
    Orphan,
    OrphanDecision,
    RefPurge,
)
from .orphan_policy import (
    FlagOrphans,
    GCOrphans,
    OrphanPolicy,
    RefuseOrphans,
)
from .integrity import (
    BasicGate,
    FieldRule,
    IntegrityGate,
    Query,
)
from .external_dep import (
    Capability,
    CapabilityRestricted,
    ExternalToolDependency,
    Restriction,
    Severity,
)
from .conformance import (
    Resolution,
    StabilityProvider,
    run_stability_conformance,
)
from .fields import (
    FIELDS,
    GENRE_VALUES,
    TENET_TAXONOMY,
    TENET_VOCAB,
    CategoricalField,
    allowed_values,
    fields_for,
    get_field,
    query_closure,
    subtree,
    validate as validate_categorical,
    writable_field_names,
)

__all__ = [
    "CatalogueError", "NotFound", "ValidationError", "Conflict",
    "IntegrityViolation", "StaleWrite", "Denied",
    "AccessMode", "Action", "Principal", "SYSTEM", "Policy", "AllowAll",
    "Ref", "Holding", "Edition", "Person", "Work", "Subject", "Collection", "Tradition",
    "WishlistItem",
    "edition_fingerprint", "person_fingerprint", "work_fingerprint", "wishlist_fingerprint",
    "Impact", "Orphan", "OrphanDecision", "RefPurge", "FileOp", "LinkRepoint", "Block",
    "OrphanPolicy", "FlagOrphans", "GCOrphans", "RefuseOrphans",
    "IntegrityGate", "BasicGate", "FieldRule", "Query",
    "Capability", "Severity", "Restriction", "ExternalToolDependency", "CapabilityRestricted",
    "Resolution", "StabilityProvider", "run_stability_conformance",
    "CategoricalField", "FIELDS", "GENRE_VALUES", "TENET_VOCAB", "TENET_TAXONOMY",
    "fields_for", "get_field", "writable_field_names", "allowed_values",
    "subtree", "query_closure", "validate_categorical",
]
