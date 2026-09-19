"""C2 benchmark tools; this package, unlike runtime Faultline, may use C5 fakes."""

from .runner import BenchmarkResult, run_hero_case
from .baselines import AmbiguityReport, NearestCentroid, evaluate_passive

__all__ = ["AmbiguityReport", "BenchmarkResult", "NearestCentroid", "evaluate_passive", "run_hero_case"]
