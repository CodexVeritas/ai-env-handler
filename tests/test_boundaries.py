"""The package layout states the trust boundaries; these tests keep the imports inside them."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def imports_cleanly(module: str, poisoned: tuple[str, ...]) -> str:
    program = (
        "import sys; sys.modules.update({name: None for name in %r}); import %s; print('ok')" % (list(poisoned), module)
    )
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    return result.stdout.strip() or result.stderr.strip().splitlines()[-1]


@pytest.mark.parametrize("module", ["envh.client.commands", "envh.client.transport", "envh.tools.importer", "envh.tools.scanner"])
def test_untrusted_side_is_stdlib_only_and_never_imports_the_trusted_side(module: str) -> None:
    assert imports_cleanly(module, ("yaml", "pyrage", "envh.core", "envh.server", "envh.install")) == "ok"


@pytest.mark.parametrize("module", ["envh.core.broker", "envh.core.config", "envh.core.vault", "envh.core.state", "envh.core.audit"])
def test_core_never_imports_process_edges_or_the_untrusted_side(module: str) -> None:
    assert imports_cleanly(module, ("envh.server", "envh.client", "envh.tools", "envh.install")) == "ok"


@pytest.mark.parametrize("module", ["envh.server.control", "envh.server.console", "envh.server.serve", "envh.server.hardening", "envh.server.init_cmd"])
def test_server_never_imports_the_untrusted_side(module: str) -> None:
    assert imports_cleanly(module, ("envh.client", "envh.tools")) == "ok"


def test_platform_and_common_are_stdlib_only() -> None:
    assert imports_cleanly("envh.platform, envh.common", ("yaml", "pyrage", "envh.core", "envh.server", "envh.client", "envh.tools")) == "ok"
