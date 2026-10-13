"""Best-effort resource release, with explicit failures and no retry policy."""


def close_resources(resources):
    """Run every close callback (returning None or error strings), in order."""
    errors = []
    for name, close in resources:
        try:
            errors.extend(f"{name}: {error}" for error in (close() or []))
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return errors
