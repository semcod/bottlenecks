"""bottlenecks – find bottlenecks in code, runtime, pipelines and host systems.

Architecture (``wellmanifest/nl-dsl-llm`` + ``nl-api-llm`` + ``nl-uri-dsl-llm``, ``uritime/urtlog``)::

    NL (PL/EN) ─► Layer 1 fast path ─► Layer 1.5 semantic cache ─► Layer 3 LLM (optional)
                                    │
    CLI / REST / MCP ─► canonical DSL ─► Engine (registry-validated, single execution boundary)
                                    │
                         analyzers: code · runtime · pipeline · system
                                    │
                 Ledger: urtlog record/v2 LOG/PLAN 4-tuples (events.jsonl + SQLite prefix index)
"""

__version__ = "0.1.0"

from .code import analyze_code, analyze_source  # noqa: E402
from .dsl import DSLCommand, DSLResult  # noqa: E402
from .engine import Engine  # noqa: E402
from .ledger import Ledger, load_events  # noqa: E402
from .models import Bottleneck, Report  # noqa: E402
from .pipeline import analyze_dag, analyze_events, analyze_pipeline, analyze_stages, critical_path  # noqa: E402
from .render import render  # noqa: E402
from .runtime import load_pstats, profile, profile_callable, profile_script  # noqa: E402
from .system import analyze_metrics, analyze_system, collect_metrics  # noqa: E402

__all__ = [
    "__version__", "Bottleneck", "Report", "render",
    "Engine", "DSLCommand", "DSLResult", "Ledger", "load_events",
    "analyze_code", "analyze_source",
    "profile", "profile_callable", "profile_script", "load_pstats",
    "analyze_dag", "analyze_stages", "analyze_pipeline", "analyze_events", "critical_path",
    "analyze_system", "analyze_metrics", "collect_metrics",
]
