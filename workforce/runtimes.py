"""Runtime discovery — enumerate installed agent CLIs as the staffing pool.

The registry is the list of well-known vendor CLI names. Detection uses
shutil.which() — the same check the engine's §4 preflight runs per shift
(engine.py:162). No credentials are checked here; keychain is a per-worker
dispatch-time concern, not a discovery signal.
"""

import os
import shutil
from typing import Dict, List, Optional

# Ordered registry of known agent CLI names.
# Add new vendors here; policy (auto-staffing, display order) stays in config.
KNOWN_RUNTIMES: List[str] = ["claude", "codex", "grok", "cursor"]


def detect(env_path: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Return {cli_name: resolved_path | None} for every known runtime.

    env_path: override the PATH search (forwarded to shutil.which as 'path').
    Runtimes not found are included with None so callers see the full registry.
    """
    return {name: shutil.which(name, path=env_path) for name in KNOWN_RUNTIMES}


def staffing_pool(
    detected: Dict[str, Optional[str]],
    roster: Optional[object] = None,
) -> List[Dict]:
    """Map detected runtimes to their employment status.

    Returns one entry per KNOWN_RUNTIMES item:
      cli     str         — the CLI name
      path    str | None  — resolved path, or None if not installed
      workers List[str]   — roster worker names whose command[0] resolves to this CLI
    """
    employed: Dict[str, List[str]] = {name: [] for name in KNOWN_RUNTIMES}
    if roster is not None:
        for wname, w in roster.workers.items():
            if w.command:
                basename = os.path.basename(w.command[0])
                if basename in employed:
                    employed[basename].append(wname)
    return [
        {"cli": name, "path": detected.get(name), "workers": employed[name]}
        for name in KNOWN_RUNTIMES
    ]
