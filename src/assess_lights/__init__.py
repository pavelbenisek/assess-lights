"""assess-lights - assess a directory of night-sky FITS light frames."""

# Single source of truth is pyproject.toml; read it back so the two cannot drift.
try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        __version__ = _pkg_version("assess-lights")
    except PackageNotFoundError:  # running from a source tree without an install
        __version__ = "0.0.0+source"
except Exception:  # noqa: BLE001
    __version__ = "0.0.0+unknown"
