"""Stage 4a: one investigator per hypothesis, each in its own clean clone (C6).

Owner 3's ``CloneInvestigator`` owns the evidence rules (reproduce, recover, predict). Product
owns the physical parts it delegates back: *how to observe a clone* (our C1 poller pointed at
the clone's ``/stats``) and *how to run the production probe inside the clone* (our C3 lever
adapter pointed at the clone's control service). Nothing here can reach production.
"""

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta

import httpx
from faultline_brain import CloneInvestigator, CloneProbe, LabExperiment
from faultline_contracts import WINDOW_S, Experiment, Fingerprint, LeverError, TriageResult, utcnow
from faultline_contracts.clone import CloneInfo, CloneLab, CloneSpec, LabError

from ..ports import HypothesisInvestigation, Investigation
from .clone import DEFAULT_RECIPES, Recipe
from .live_telemetry import FingerprintWriter, LiveTelemetrySource, TelemetryUnavailable
from .sandbox import SandboxLeverAdapter

log = logging.getLogger(__name__)


class FixtureInvestigation:
    def investigate(self, incident_id, triage, production_incident, healthy_reference, production_probe):
        return [
            HypothesisInvestigation(
                hypothesis_id=h.id,
                clone_id=f"fixture-{h.id.lower()}",
                recipe=DEFAULT_RECIPES.get(h.id),
                reproduced=True,
                recovered=True,
                prediction_matches=1,
                prediction_total=1,
                detail="fixture clone: reproduced, recovered, predicted",
            )
            for h in triage.hypotheses
        ]


class _CleanupTolerantLab:
    """Owner 3's investigator resets then destroys the clone in a ``finally``; a reset that
    cannot reach a healthy baseline (503) must not discard the evidence already collected,
    and the destroy right after makes the reset's outcome moot."""

    def __init__(self, lab: CloneLab):
        self._lab = lab

    def __getattr__(self, name):
        return getattr(self._lab, name)

    def reset(self, clone_id: str):
        try:
            return self._lab.reset(clone_id)
        except (LabError, httpx.HTTPError) as exc:
            log.warning("clone %s reset failed before destroy: %s", clone_id, exc)
            return self._lab.get(clone_id)

    def destroy(self, clone_id: str):
        try:
            return self._lab.destroy(clone_id)
        except (LabError, httpx.HTTPError) as exc:
            log.warning("clone %s destroy failed: %s", clone_id, exc)
            return None


class LabInvestigation(Investigation):
    """Runs Owner 3's investigator for every hypothesis that has a reproduction recipe,
    ``max_clones`` at a time (production + 2 clones is the budget).

    ``settle_s`` is how long a clone gets after an injection before it is sampled; the storm
    needs ~10 s past its trigger to be self-sustaining (sandbox/INTEGRATION.md).
    """

    def __init__(
        self,
        lab: CloneLab,
        recipes: dict[str, Recipe] | None = None,
        max_clones: int = 1,
        settle_s: float = 15,
        probe_watch_s: float = 20,
        writer: FingerprintWriter | None = None,
        telemetry_factory: Callable[[CloneInfo, str], LiveTelemetrySource] | None = None,
        levers_factory: Callable[[CloneInfo], SandboxLeverAdapter] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
    ):
        self._lab = lab
        self._recipes = recipes or DEFAULT_RECIPES
        self._max_clones = max_clones
        self._settle_s = settle_s
        self._probe_watch_s = probe_watch_s
        self._writer = writer
        self._telemetry_factory = telemetry_factory or self._clone_telemetry
        self._levers_factory = levers_factory or _clone_levers
        self._sleep = sleep
        self._clock = clock

    def investigate(
        self,
        incident_id: str,
        triage: TriageResult,
        production_incident: Fingerprint,
        healthy_reference: list[Fingerprint],
        production_probe: Experiment,
    ) -> list[HypothesisInvestigation]:
        healthy = healthy_reference[-1] if healthy_reference else production_incident
        jobs = [
            (h.id, self._recipes[h.id]) for h in triage.hypotheses if h.id in self._recipes
        ]
        skipped = [
            HypothesisInvestigation(h.id, None, None, False, False, None, None,
                                    "no reproduction recipe; not investigated")
            for h in triage.hypotheses if h.id not in self._recipes
        ]
        if not jobs:
            return skipped
        with ThreadPoolExecutor(max_workers=max(1, min(self._max_clones, len(jobs)))) as pool:
            results = list(
                pool.map(
                    lambda job: self._one(incident_id, triage, job[0], job[1], production_incident, healthy, production_probe),
                    jobs,
                )
            )
        return results + skipped

    # -- one hypothesis ---------------------------------------------------------------------

    def _one(
        self,
        incident_id: str,
        triage: TriageResult,
        hypothesis_id: str,
        recipe: Recipe,
        production_incident: Fingerprint,
        healthy: Fingerprint,
        production_probe: Experiment,
    ) -> HypothesisInvestigation:
        sources: dict[str, LiveTelemetrySource] = {}

        def observe(clone: CloneInfo) -> Fingerprint:
            source = self._source(sources, clone, incident_id)
            self._sleep(self._settle_s)
            latest = source.latest()
            if latest is None:
                raise TelemetryUnavailable(f"no telemetry from clone {clone.clone_id}")
            return latest

        def run_probe(clone: CloneInfo, probe: Experiment) -> CloneProbe:
            return self._probe(sources, clone, incident_id, recipe, probe)

        experiment = LabExperiment(recipe["action"], recipe["params"], recipe["ttl_s"])
        spec = CloneSpec(name=_clone_name(incident_id, hypothesis_id))
        try:
            evidence = CloneInvestigator(_CleanupTolerantLab(self._lab), observe).investigate(
                hypothesis_id, spec, production_incident, healthy, experiment,
                triage=triage, production_probe=production_probe, run_probe=run_probe,
            )
        except (LabError, LeverError, TelemetryUnavailable, httpx.HTTPError, RuntimeError, ValueError) as exc:
            return HypothesisInvestigation(
                hypothesis_id, next(iter(sources), None), recipe, False, False, None, None,
                f"investigation aborted: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - a clone problem must never take the production loop down
            log.exception("investigator %s crashed", hypothesis_id)
            return HypothesisInvestigation(
                hypothesis_id, next(iter(sources), None), recipe, False, False, None, None,
                f"investigation crashed: {type(exc).__name__}: {exc}",
            )
        finally:
            for source in sources.values():
                source.stop()
        pred = evidence.prediction
        if pred is not None and pred.measured_expectations == 0:
            pred = None  # probe ran but nothing could be measured (e.g. clone reset failed)
        detail = (
            f"{'reproduced' if evidence.reproduction.reproduced else 'did not reproduce'} the production "
            f"fingerprint ({evidence.reproduction.similarity.matching_metrics}/"
            f"{evidence.reproduction.similarity.shared_metrics} metrics within noise); "
            f"{'recovered' if evidence.recovery.recovered else 'did not recover'} once the cause was removed"
        )
        if pred is not None:
            detail += f"; {production_probe.id} in the clone matched {pred.matched_expectations}/{pred.measured_expectations} predicted directions"
        else:
            detail += f"; {production_probe.id} not measured in the clone"
        return HypothesisInvestigation(
            hypothesis_id=hypothesis_id,
            clone_id=evidence.reproduction.clone_id,
            recipe=recipe,
            reproduced=evidence.reproduction.reproduced,
            recovered=evidence.recovery.recovered,
            prediction_matches=pred.matched_expectations if pred else None,
            prediction_total=pred.measured_expectations if pred else None,
            detail=detail,
            evidence={
                "reproduction": asdict(evidence.reproduction.similarity),
                "recovery": asdict(evidence.recovery.similarity),
                "prediction": asdict(pred) if pred else None,
            },
        )

    def _probe(
        self,
        sources: dict[str, LiveTelemetrySource],
        clone: CloneInfo,
        incident_id: str,
        recipe: Recipe,
        probe: Experiment,
    ) -> CloneProbe:
        """Reset the clone to a verified healthy state, re-create the incident, then run the
        production probe against it exactly as production would: apply -> hold -> undo -> watch.

        The reset matters: after the reproduction step the clone may still be in a
        self-sustaining storm (removing the trigger does not end a metastable failure), and the
        probe's healthy baseline must be measured on a clone that is actually healthy."""
        source = self._source(sources, clone, incident_id)
        levers = self._levers_factory(clone)
        try:
            self._lab.reset(clone.clone_id)
        except (LabError, httpx.HTTPError) as exc:
            # No verified-healthy baseline -> the probe cannot be judged; report it unmeasured
            # rather than scoring directions against a storm baseline.
            log.warning("clone %s could not be reset before the probe: %s", clone.clone_id, exc)
            return CloneProbe([], [], [], [])
        self._sleep(4 * WINDOW_S)
        t_healthy_end = self._clock()
        healthy_baseline = source.series(t_healthy_end - timedelta(seconds=4 * WINDOW_S), t_healthy_end)

        cause = self._lab.apply(clone.clone_id, recipe["action"], recipe["params"], recipe["ttl_s"])
        self._sleep(recipe["ttl_s"] + self._settle_s)  # let the trigger end and the incident sustain itself
        t_incident_end = self._clock()
        incident_baseline = source.series(t_incident_end - timedelta(seconds=4 * WINDOW_S), t_incident_end)

        handle = levers.apply(probe.lever_id, probe.params, probe.hold_s + 30)
        self._sleep(probe.hold_s)
        t_release = self._clock()
        levers.undo(handle)
        self._sleep(self._probe_watch_s)
        t_end = self._clock()
        try:
            self._lab.undo(cause)  # persistent causes (db_capacity) must not outlive the probe
        except LabError:
            pass
        return CloneProbe(
            healthy_baseline=healthy_baseline,
            incident_baseline=incident_baseline,
            during=source.series(t_incident_end, t_release),
            after_release=source.series(t_release, t_end),
        )

    def _source(self, sources, clone: CloneInfo, incident_id: str) -> LiveTelemetrySource:
        source = sources.get(clone.clone_id)
        if source is None:
            source = self._telemetry_factory(clone, incident_id)
            source.start()
            sources[clone.clone_id] = source
        return source

    def _clone_telemetry(self, clone: CloneInfo, incident_id: str) -> LiveTelemetrySource:
        urls = clone.endpoints.stats_urls
        return LiveTelemetrySource(
            orders_url=urls["orders"],
            payments_url=urls["payments"],
            loadgen_url=urls["loadgen"],
            orders_v2_url=urls.get("orders-v2"),
            writer=self._writer,
            incident_id=incident_id,
            clone_id=clone.clone_id,
        )


def _clone_levers(clone: CloneInfo) -> SandboxLeverAdapter:
    # A clone's control service proxies into Orders, which is saturated during a storm; 5 s is
    # not enough there (seen live), while production's control answered within it.
    return SandboxLeverAdapter(base_url=clone.endpoints.control_url, timeout_s=20.0)


def _clone_name(incident_id: str, hypothesis_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in f"{hypothesis_id}-{incident_id}".lower())
    return safe[:32].rstrip("-") or "investigate"


__all__ = ["FixtureInvestigation", "LabInvestigation"]
