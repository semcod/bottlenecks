"""bottlenecks – find bottlenecks in code, runtime, pipelines and host systems."""

__version__ = "0.1.0"

from .code import analyze_code, analyze_source  # noqa: E402
from .models import Bottleneck, Report  # noqa: E402
from .pipeline import analyze_dag, analyze_pipeline, analyze_stages, critical_path  # noqa: E402
from .render import render  # noqa: E402
from .runtime import load_pstats, profile, profile_callable, profile_script  # noqa: E402
from .system import analyze_metrics, analyze_system, collect_metrics  # noqa: E402

__all__ = [
    "__version__", "Bottleneck", "Report", "render",
    "analyze_code", "analyze_source",
    "profile", "profile_callable", "profile_script", "load_pstats",
    "analyze_dag", "analyze_stages", "analyze_pipeline", "critical_path",
    "analyze_system", "analyze_metrics", "collect_metrics",
]
