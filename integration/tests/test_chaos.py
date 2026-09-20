"""Chaos suite: the real orchestrator + real Brain math over FakeWorld, one test per case × seed.

  cd integration && uv run pytest -q tests/test_chaos.py                 # whole catalogue (~1 min)
  cd integration && uv run pytest -q tests/test_chaos.py -k lever        # one family
  cd integration && uv run python -m chaos                               # table + JSON report in runs/

Every case asserts the safety invariants in `chaos.harness.check_invariants`; cases with an
expected diagnosis / terminal state assert those too. A case whose expectation the system is
known to miss is `xfail(strict=True)` with the gap named, so fixing it flips the test red until
the marker is removed.
"""

import pytest

from chaos import ALL_CASES, run_case
from chaos.harness import FINALS

PARAMS = [
    pytest.param(case, seed, id=f"{case.id}-s{seed}",
                 marks=[pytest.mark.xfail(strict=True, reason=case.known_gap)] if case.known_gap else [])
    for case in ALL_CASES
    for seed in case.seeds
]


@pytest.mark.parametrize("case,seed", PARAMS)
def test_chaos_case(case, seed):
    outcome = run_case(case, seed)
    detail = "\n".join(
        [f"case: {case.id} ({case.story})", f"hidden world: {outcome.hidden_world}",
         f"final={outcome.final} diagnosis={outcome.diagnosis} raised={outcome.raised}", *outcome.output]
    )
    assert outcome.final in FINALS
    assert not outcome.invariant_failures, f"safety invariants violated: {outcome.invariant_failures}\n{detail}"
    assert outcome.final in case.expect_final, f"unexpected terminal state\n{detail}"
    assert case.expected(outcome.final, outcome.diagnosis), f"unexpected diagnosis\n{detail}"


def test_catalogue_is_well_formed():
    for case in ALL_CASES:
        assert case.expect_final <= set(FINALS), case.id
        assert case.seeds, case.id
