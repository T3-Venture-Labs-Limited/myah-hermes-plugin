"""Root __init__.py for `hermes plugins install` (directory-style).

When `hermes plugins install T3-Venture-Labs-Limited/myah-hermes-plugin`
runs, hermes does:

  1. `git clone --depth 1 git+https://github.com/T3-Venture-Labs-Limited/myah-hermes-plugin /tmp/xxx`
  2. Read `/tmp/xxx/plugin.yaml` -> `name: myah`
  3. `shutil.move /tmp/xxx ~/.hermes/plugins/myah/`

Then on hermes startup, the plugin loader does:

  4. `spec = importlib.util.spec_from_file_location('myah', '~/.hermes/plugins/myah/__init__.py')`
  5. `module = importlib.util.module_from_spec(spec)`
  6. `spec.loader.exec_module(module)`
  7. `module.register(ctx)` — calls into the function below

The actual platform-adapter / tool / runtime-admin wiring lives under
`myah_hermes_plugin/myah_platform/`. We expose a thin `register(ctx)`
shim here that delegates.

Why `sys.path.insert`: when the plugin is loaded via
`spec_from_file_location`, only the file's own dir scope is set up — the
sub-package `myah_hermes_plugin` is NOT automatically importable. Adding
the plugin-root dir to `sys.path` makes
`from myah_hermes_plugin.myah_platform import register` resolve cleanly.

The pyproject `[project.entry-points."hermes_agent.plugins"]` block still
exists as a fallback for developers who pip-install the plugin directly
into their hermes venv (e.g. `pip install -e .` from a clone). That path
finds `register` via entry-points discovery, not via this root __init__.py.
Both paths must reach the same underlying register function — see test
`tests/test_directory_style_install.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the cloned-dir layout (which contains `myah_hermes_plugin/`) is
# on sys.path so the relative-style import below works regardless of how
# hermes' plugin loader invoked us.
_HERE = Path(__file__).parent
_HERE_STR = str(_HERE)
if _HERE_STR not in sys.path:
    sys.path.insert(0, _HERE_STR)

# The actual platform-adapter / tools / runtime-admin wiring lives here.
# This import has to succeed at module-load time so the plugin loader's
# `getattr(module, 'register')` finds the function below.
from myah_hermes_plugin.myah_platform import register as _myah_platform_register  # noqa: E402


def register(ctx) -> None:
    """Hermes plugin entry point — delegates to myah_hermes_plugin.myah_platform.register.

    This is the function the hermes plugin loader calls. It must accept the
    plugin context object and return None (per upstream's plugin contract at
    hermes_cli/plugins.py:1042).
    """
    return _myah_platform_register(ctx)


__all__ = ['register']
