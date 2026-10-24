"""Validate verified-TLS MySQL URLs without exposing credentials."""
from urllib.parse import parse_qs, urlsplit


def is_mysql_database_url(value):
    try:
        parsed = urlsplit(value or "")
        host = (parsed.hostname or "").lower().rstrip(".")
        query = {key.lower().replace("_", "-"): values[-1].lower()
                 for key, values in parse_qs(parsed.query).items() if values}
        ssl_mode = query.get("sslmode") or query.get("ssl-mode")
        verified_tls = ssl_mode in {"verify-ca", "verify-full", "verify-identity", "verify_identity"}
        verified_tls = verified_tls or (query.get("ssl-verify-cert") == "true"
                                        and query.get("ssl-verify-identity") == "true")
        return (parsed.scheme in {"mysql", "mysql+pymysql"} and bool(host)
                and bool(parsed.username) and bool(parsed.password)
                and bool(parsed.path.strip("/")) and verified_tls)
    except (TypeError, ValueError):
        return False
