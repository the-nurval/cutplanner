"""Signed, expiring shared-login session tokens. Never stores a password in cookies."""
import base64
import hashlib
import hmac
import json
import time

COOKIE_NAME = 'cutplanner_team_session'
SESSION_HOURS = 12


def issue_token(username: str, password: str, secret: str, now=None) -> str:
    payload = {'u': username, 'exp': int(time.time() if now is None else now) + SESSION_HOURS * 3600,
               'v': hashlib.sha256(password.encode()).hexdigest()[:16]}
    data = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
    signature = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f'{data}.{signature}'


def valid_token(token: str, username: str, password: str, secret: str, now=None) -> bool:
    try:
        data, signature = token.split('.', 1)
        expected = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        obj = json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
        return (hmac.compare_digest(str(obj['u']), username)
                and hmac.compare_digest(str(obj['v']), hashlib.sha256(password.encode()).hexdigest()[:16])
                and int(obj['exp']) > int(time.time() if now is None else now))
    except (ValueError, KeyError, TypeError, OverflowError):
        return False
