"""What a candidate binding reads, shown to the reviewer before APPROVE_BINDING (ADR-0005)."""

from dataproof_contracts._base import Contract, NonEmptyText, Sha256Hex
from dataproof_contracts.enums import RelationProvenance


class BindingRelation(Contract):
    """A relation the binding reads, directly or through the views listed in ``through_views``."""

    name: NonEmptyText
    provenance: RelationProvenance
    through_views: tuple[NonEmptyText, ...] = ()

    @property
    def flagged(self) -> bool:
        """Pre-existing and untouched by the PR: a prompt for the reviewer, not a rejection."""
        return self.provenance is RelationProvenance.UNCHANGED


class BindingAnalysis(Contract):
    """Relations and functions a binding uses, as resolved by PostgreSQL on the candidate."""

    binding_digest: Sha256Hex
    relations: tuple[BindingRelation, ...]
    functions: tuple[NonEmptyText, ...]

    def flagged(self) -> tuple[BindingRelation, ...]:
        return tuple(relation for relation in self.relations if relation.flagged)
