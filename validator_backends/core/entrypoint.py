"""Choose one-shot Job mode or bounded HTTP Service mode for one image."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from validator_backends.core.service_runtime import SERVICE_SHAPE_ENV


def main(argv: list[str] | None = None) -> int:
    """Execute the image's sole backend using the configured provider shape."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-module", required=True)
    args = parser.parse_args(argv)
    if os.getenv(SERVICE_SHAPE_ENV, "").strip().lower() == "service":
        from validator_backends.core.service_runtime import main as service_main

        return service_main(["--backend-module", args.backend_module])
    return subprocess.call([sys.executable, "-m", args.backend_module])


if __name__ == "__main__":
    raise SystemExit(main())
