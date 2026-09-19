from faultline_product.ports import PatchProposal


class FixtureDevinAdapter:
    """Deterministic stand-in for the future Devin integration."""

    def propose(self, incident_id: str, diagnosis: str) -> PatchProposal:
        return PatchProposal(
            provider="devin-fixture",
            reference=f"devin://task/{incident_id}",
            summary=f"Add bounded exponential backoff and jitter for {diagnosis}",
        )
