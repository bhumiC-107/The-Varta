"""Validation for Neon targets; never formats or returns credential-bearing DSNs."""
from urllib.parse import parse_qs, urlsplit


def is_neon_database_url(value):
    try:
        parsed = urlsplit(value or "")
        host = (parsed.hostname or "").lower().rstrip(".")
        sslmode = parse_qs(parsed.query).get("sslmode", [""])[0].lower()
        return (parsed.scheme in {"postgres", "postgresql"}
                and bool(parsed.username) and bool(parsed.password)
                and host.endswith(".neon.tech")
                and sslmode in {"require", "verify-ca", "verify-full"})
    except (TypeError, ValueError):
        return False
