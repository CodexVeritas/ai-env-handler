"""Trusted logic that runs inside the broker as user envh: policy, vault, state, decisions, audit.

Pure except for the vault and audit files. Imports nothing from envh.server, envh.client or envh.tools, so it can be
reviewed on its own; a test enforces that.
"""
