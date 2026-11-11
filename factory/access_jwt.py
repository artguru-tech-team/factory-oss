"""Check the Cloudflare Access login on requests that came through the tunnel.

Cloudflare Access adds a signed JWT in Cf-Access-Jwt-Assertion on tunnelled requests.
Team domain and audience tag are configured via config keys `access.team` and `access.aud`.
`access.machine_aud` (optional) is the audience of a second Access app that covers only the machines
push path with a service-token policy, so a machine's token opens nothing else at the edge.
Requests from this machine (agents on 127.0.0.1, no Cloudflare headers) skip the check
and keep using ?k=. Fails closed: a tunnelled request (carrying Cf-Ray or Cf-Connecting-Ip)
is refused when Access is not configured or without a valid token.
"""
import jwt

from . import config

_jwks_clients = {}


def _get_jwks(team):
    url = team.rstrip("/") + "/cdn-cgi/access/certs"
    if url not in _jwks_clients:
        _jwks_clients[url] = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)
    return _jwks_clients[url]


def claims(headers):
    """The verified Access JWT payload, or None when it is missing or invalid."""
    try:
        team, aud = config.get("access.team"), config.get("access.aud")
    except Exception:  # corrupt config.json: refuse, and keep its path out of the answer
        return None
    if not isinstance(team, str) or not team.startswith("https://") or not isinstance(aud, str) or not aud:
        return None
    team = team.rstrip("/")
    token = headers.get("Cf-Access-Jwt-Assertion", "")
    try:
        key = _get_jwks(team).get_signing_key_from_jwt(token).key
        machine_aud = config.get("access.machine_aud")
        auds = [aud] + ([machine_aud] if isinstance(machine_aud, str) and machine_aud else [])
        return jwt.decode(token, key, algorithms=["RS256"], audience=auds, issuer=team)
    except Exception:
        return None


def allowed(headers):
    if not (headers.get("Cf-Ray") or headers.get("Cf-Connecting-Ip")):
        return True
    return claims(headers) is not None


def is_person(headers):
    """True for a person's Access login (it carries an email). A service token (a machine's
    collector) has no email and may only push its agents."""
    c = claims(headers)
    if not (c and c.get("email")):
        return False
    aud = c.get("aud")
    return config.get("access.aud") in (aud if isinstance(aud, list) else [aud])
