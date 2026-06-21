"""Packaging entry-point package for the Briefcase app (macOS/Windows/Linux).

The real application lives in the `gui` package; this thin package exists only so the
Briefcase app module name (`telegram_export`) matches a bundled source package and
`python -m telegram_export` (see __main__.py) launches the GUI. The version of record
is in pyproject.toml ([tool.briefcase].version) — intentionally not duplicated here.
"""
