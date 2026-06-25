#!/usr/bin/env python3
"""Portable / PyInstaller entry point for iAmped.

The same thing `python -m iamped.app` does, but as a top-level script so
PyInstaller has a clean entry point to freeze into a single app.
"""
from iamped.app import main

if __name__ == "__main__":
    raise SystemExit(main())
