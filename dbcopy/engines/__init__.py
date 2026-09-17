"""Engines backed by a native Python driver rather than a CLI tool.

``dbcopy.adapters`` wraps the vendors' own dump/restore programs (decision 1);
this package is for engines where that is not an option. MongoDB lives here
because the Database Tools cannot remap-and-stream a copy reliably, so the
copy is driven through pymongo instead.

Unlike the rest of the package, modules under ``dbcopy/engines`` may import
third-party libraries. Importing this package does not pull them in — the
submodule that needs a driver imports it, and callers import that submodule
lazily, so PostgreSQL and MySQL keep working with nothing extra installed.
"""
