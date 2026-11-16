"""Static Polars performance checks. Target source is never executed."""

from datawrench import __version__

from .analysis import AnalysisResult, Diagnostic, Suppression, analyze, analyze_with_suppressions, fix_source

__all__ = ["AnalysisResult", "Diagnostic", "Suppression", "analyze", "analyze_with_suppressions", "fix_source"]
