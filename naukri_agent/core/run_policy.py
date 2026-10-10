"""Immutable safety policy enforced at external side-effect boundaries."""

from __future__ import annotations

from dataclasses import dataclass


class SideEffectBlocked(RuntimeError):
    """Raised when an external mutation is forbidden by the active run policy."""


@dataclass(frozen=True, slots=True)
class RunPolicy:
    """Single source of truth for whether a run may mutate external systems."""

    dry_run: bool
    side_effects_enabled: bool = True

    @property
    def may_mutate(self) -> bool:
        return self.side_effects_enabled and not self.dry_run

    def require_mutation(self, action: str) -> None:
        if not self.may_mutate:
            reason = "dry-run" if self.dry_run else "global kill switch"
            raise SideEffectBlocked(f"{action} blocked by {reason}")
