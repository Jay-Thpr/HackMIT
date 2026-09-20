from datetime import timedelta

from faultline_brain import InvestigatorAgent


def production_evidence(reader, incident_id, fingerprint):
    try:
        end = fingerprint.window_end
        start = end - timedelta(seconds=120)
        context = reader.context(incident_id, start, end)
        context["similar_incidents"] = reader.similar_incidents(
            fingerprint, incident_id=incident_id, before=start
        )
        return context
    except Exception as exc:
        return {
            "source": "primary_elasticsearch",
            "status": "unavailable",
            "reason": type(exc).__name__,
        }


def evidence_references(context) -> list[str]:
    refs: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "reference" and isinstance(value, str):
                    refs.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(context)
    return sorted(refs)


def evidence_metadata(context) -> dict:
    return {
        "evidence_status": context.get("status"),
        "history_status": (context.get("similar_incidents") or {}).get("status"),
        "references": evidence_references(context),
        "scope": context.get("scope"),
    }


class EvidenceInvestigatorAgent:
    def __init__(self, client, reader, incident_id, hypothesis_id, clone, *, model, clock, provenance_sink=None):
        self._client = client
        self._reader = reader
        self._incident_id = incident_id
        self._hypothesis_id = hypothesis_id
        self._clone = clone
        self._model = model
        self._clock = clock
        self._provenance_sink = provenance_sink

    def propose(self, hypothesis, catalog, production_incident, healthy, history, attempts_left):
        production_context = production_evidence(self._reader, self._incident_id, production_incident)
        try:
            end = self._clock()
            start = max(self._clone.created_at, end - timedelta(seconds=120))
            clone_context = (
                self._reader.context(self._incident_id, start, end, clone_id=self._clone.clone_id)
                if start < end
                else {"status": "empty", "timeline": {"items": []}, "audit": {"items": []}}
            )
        except Exception as exc:
            clone_context = {
                "source": "primary_elasticsearch",
                "status": "unavailable",
                "reason": type(exc).__name__,
            }
        context = {
            "production": production_context,
            "clone": clone_context,
            "hypothesis_id": self._hypothesis_id,
            "clone_id": self._clone.clone_id,
        }
        sink = self._sink()
        if sink is not None:
            sink({'provider': 'agent_builder', 'role': 'investigator', 'status': 'evidence_loaded',
                  'production': evidence_metadata(production_context),
                  'clone': evidence_metadata(clone_context)})
        client = self._client.with_context(context, provenance_sink=sink)
        return InvestigatorAgent(client, model=self._model).propose(
            hypothesis, catalog, production_incident, healthy, history, attempts_left
        )

    def _sink(self):
        if self._provenance_sink is None:
            return None

        def sink(metadata: dict) -> None:
            self._provenance_sink(
                {
                    **metadata,
                    "hypothesis_id": self._hypothesis_id,
                    "clone_id": self._clone.clone_id,
                }
            )

        return sink
