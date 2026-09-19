"""C2 benchmark tools; this package, unlike runtime Faultline, may use C5 fakes."""

from .runner import BenchmarkResult, run_hero_case

__all__ = ["BenchmarkResult", "run_hero_case"]
