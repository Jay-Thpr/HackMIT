from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PatchProposal:
    provider: str
    reference: str
    summary: str


@runtime_checkable
class PatchAdapter(Protocol):
    def propose(self, incident_id: str, diagnosis: str) -> PatchProposal: ...
