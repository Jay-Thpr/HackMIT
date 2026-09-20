"""C2 benchmark tools; this package, unlike runtime Faultline, may use C5 fakes."""

from .runner import BenchmarkResult, run_hero_case, run_llm_only_case, run_passive_only_case, run_random_case
from .baselines import AmbiguityReport, NearestCentroid, evaluate_passive
from .live import LiveCase, LiveWorld, build_plan, diagnosis_from_audit, score
from .suite import Scenario, SuiteReport, SuiteRun, run_frozen_suite

__all__ = ["AmbiguityReport", "BenchmarkResult", "LiveCase", "LiveWorld", "NearestCentroid", "Scenario", "SuiteReport", "SuiteRun", "build_plan", "diagnosis_from_audit", "evaluate_passive", "run_frozen_suite", "run_hero_case", "run_llm_only_case", "run_passive_only_case", "run_random_case", "score"]
