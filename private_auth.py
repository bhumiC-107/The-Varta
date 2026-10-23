"""Small single-owner session and origin checks for the Vercel handler."""
import base64
import hashlib
import hmac
import os
import secrets
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

COOKIE_NAME = "__Host-morning-signal"
SESSION_SECONDS = 12 * 60 * 60


class AuthConfigurationError(ValueError):
    pass


def configuration(env=None):
    env = env or os.environ
    username = env.get("MORNING_SIGNAL_USERNAME", "")
    password = env.get("MORNING_SIGNAL_PASSWORD", "")
    secret = env.get("MORNING_SIGNAL_SESSION_SECRET", "")
    origin = env.get("APP_ORIGIN", "")
    if not username or len(username) > 128 or len(password) < 16 or len(password) > 1024 or len(secret) < 32:
        raise AuthConfigurationError("Private access is not configured")
    try:
        parsed = urlsplit(origin)
    except ValueError as ex:
        raise AuthConfigurationError("Private access is not configured") from ex
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise AuthConfigurationError("Private access is not configured")
    canonical_origin = f"https://{parsed.netloc.lower()}"
    return username, password, secret.encode("utf-8"), canonical_origin


def authenticate(username, password, env=None):
    expected_user, expected_password, _, _ = configuration(env)
    user_matches = hmac.compare_digest(str(username or ""), expected_user)
    password_matches = hmac.compare_digest(str(password or ""), expected_password)
    return user_matches & password_matches


def create_session(secret, now=None):
    expires = int((now if now is not None else time.time()) + SESSION_SECONDS)
    csrf = secrets.token_urlsafe(32)
    payload = f"{expires}.{csrf}".encode("ascii")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(secret, encoded, hashlib.sha256).digest()
    return (encoded + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode("ascii"), csrf, expires


def session_from_cookie(cookie_header, secret, now=None):
    try:
        if len(cookie_header or "") > 2048:
            return None
        cookies = SimpleCookie()
        cookies.load(cookie_header or "")
        value = cookies[COOKIE_NAME].value
        encoded, signature = value.encode("ascii").split(b".", 1)
        supplied = base64.urlsafe_b64decode(signature + b"=" * (-len(signature) % 4))
        expected = hmac.new(secret, encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            return None
        payload = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)).decode("ascii")
        expires_text, csrf = payload.split(".", 1)
        expires = int(expires_text)
        if expires <= int(now if now is not None else time.time()) or len(csrf) < 32:
            return None
        return {"csrf": csrf, "expires": expires}
    except (KeyError, ValueError, TypeError, UnicodeError, base64.binascii.Error):
        return None


def origin_matches(headers, expected_origin, require_origin=False):
    origin = headers.get("Origin")
    if origin:
        try:
            parsed_origin = urlsplit(origin)
        except ValueError:
            return False
        if (parsed_origin.scheme != "https" or not parsed_origin.netloc or parsed_origin.path not in ("", "/")
                or parsed_origin.query or parsed_origin.fragment or parsed_origin.username or parsed_origin.password):
            return False
        supplied_origin = f"https://{parsed_origin.netloc.lower()}"
        if not hmac.compare_digest(supplied_origin, expected_origin):
            return False
    elif require_origin:
        return False
    if headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return False
    expected_host = urlsplit(expected_origin).netloc.lower()
    request_host = (headers.get("Host") or headers.get("x-forwarded-host") or "").split(",", 1)[0].strip().lower()
    if request_host.endswith(":443") and not expected_host.endswith(":443"):
        request_host = request_host[:-4]
    return bool(request_host) and hmac.compare_digest(request_host, expected_host)


def cookie_header(value, max_age=SESSION_SECONDS):
    return f"{COOKIE_NAME}={value}; Path=/; Max-Age={int(max_age)}; Secure; HttpOnly; SameSite=Strict"
