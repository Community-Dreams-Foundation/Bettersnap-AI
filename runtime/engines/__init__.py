"""Runtime engine implementations (GPU worker).

Each class implements a domain engine Protocol (domain/engines.py) and holds its runtime
dependencies — loaded models via the PipelineContext, config, and a log callable — injected at
construction. No engine imports main.py; no engine calls another engine (the orchestrator owns
the workflow). See ARCHITECTURE.md §4 and the Phase 2 rules.
"""
