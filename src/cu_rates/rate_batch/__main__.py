"""Executable module entrypoint for `python -m cu_rates.rate_batch`."""

import sys

from cu_rates.rate_batch.cli import main

if __name__ == "__main__":
    sys.exit(main())
