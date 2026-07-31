"""
Naukri.com auto-apply agent.

Layered on purpose, with dependencies pointing one way only:

    cli / scheduler        entrypoints, exit codes, cron
      -> core.orchestrator run lifecycle and safety valves
        -> naukri.*        Naukri-specific flows (auth, search, apply, chatbot)
          -> browser.*     Playwright lifecycle, resilience primitives, artifacts
          -> db.*          asyncpg pool, schema, repository
            -> core.models plain dataclasses shared by every layer

`config` and `logging_setup` are leaves that every layer may import. Nothing in
`core.models`, `core.filters`, `core.answers` or `naukri.parser` touches
Playwright or SQL, which is what makes the decision logic unit-testable.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
