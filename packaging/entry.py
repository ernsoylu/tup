"""PyInstaller entry point for the tup single-file executable."""

import multiprocessing

from tup.cli import app

if __name__ == "__main__":
    # Frozen binaries re-exec themselves for multiprocessing workers; without
    # this guard each worker would relaunch the CLI instead.
    multiprocessing.freeze_support()
    app()
