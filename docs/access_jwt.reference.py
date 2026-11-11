"""Check the Cloudflare Access login on requests that came through the tunnel.

Cloudflare Access (app "Factory dashboards", team example.cloudflareaccess.com) logs
the owner in before the tunnel; it adds a signed JWT in Cf-Access-Jwt-Assertion. Requests
from this machine (agents on 127.0.0.1, no Cloudflare headers) skip the check and keep
using ?k=. Fails closed: a tunnelled request without a valid token is refused.
Copied verbatim in ~/ds-pages and ~/c2e-pages; keep both in sync.
"""
import jwt

TEAM = "https://example.cloudflareaccess.com"
AUD = "0000000000000000000000000000000000000000000000000000000000000000"
_jwks = jwt.PyJWKClient(TEAM + "/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)


def allowed(headers):
    if not (headers.get("Cf-Ray") or headers.get("Cf-Connecting-Ip")):
        return True
    token = headers.get("Cf-Access-Jwt-Assertion", "")
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        jwt.decode(token, key, algorithms=["RS256"], audience=AUD, issuer=TEAM)
        return True
    except Exception:
        return False
