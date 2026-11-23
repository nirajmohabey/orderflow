"""Fixed-origin Neon authentication and bounded cached Ed25519 verification."""
from http.cookies import CookieError, SimpleCookie
import json
import os
import re
import threading
import time
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
import jwt

MAX_JWKS = 65536
MAX_PROXY_REQUEST = 32768
MAX_PROXY_RESPONSE = 262144
_cache = {}
_attempts = {}
_cache_lock = threading.Lock()


class NeonAuthError(ValueError):
    pass


def configuration():
    base = os.environ.get("NEON_AUTH_URL", "").rstrip("/")
    if not base:
        return None
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".neon.tech")
            or parsed.username or parsed.password or parsed.port not in {None,443} or parsed.query or parsed.fragment
            or not parsed.path.endswith("/auth") or ".." in parsed.path):
        raise NeonAuthError("Invalid fixed Neon authentication URL")
    origin = urlunsplit((parsed.scheme,parsed.netloc,"","",""))
    jwks = os.environ.get("NEON_AUTH_JWKS_URL", base + "/.well-known/jwks.json")
    key_url = urlsplit(jwks)
    if (urlunsplit((key_url.scheme,key_url.netloc,"","","")) != origin or key_url.username or key_url.password
            or key_url.query or key_url.fragment or not key_url.path.startswith(parsed.path + "/")
            or not key_url.path.endswith("/.well-known/jwks.json")):
        raise NeonAuthError("Invalid fixed Neon key URL")
    return {"base":base,"origin":origin,"jwks":jwks}


def _fetch_keys(url):
    with httpx.Client(timeout=5,trust_env=False,follow_redirects=False) as client:
        with client.stream("GET",url,headers={"Accept":"application/json","Accept-Encoding":"identity"}) as response:
            if response.status_code != 200 or response.headers.get("content-encoding", "identity") not in {"identity",""}:
                raise NeonAuthError("Neon keys unavailable")
            body = bytearray()
            for chunk in response.iter_bytes():
                if len(chunk) > MAX_JWKS-len(body): raise NeonAuthError("Oversized Neon key set")
                body.extend(chunk)
    value=json.loads(body)
    keys=value.get("keys") if isinstance(value,dict) else None
    if not isinstance(keys,list) or not 1 <= len(keys) <= 16: raise NeonAuthError("Invalid Neon key set")
    result={}
    for key in keys:
        kid=key.get("kid") if isinstance(key,dict) else None
        if (not isinstance(kid,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",kid) or kid in result
                or key.get("kty")!="OKP" or key.get("crv")!="Ed25519" or key.get("alg","EdDSA")!="EdDSA"
                or "d" in key or key.get("use","sig")!="sig" or ("key_ops" in key and "verify" not in key["key_ops"])):
            raise NeonAuthError("Invalid Neon signing key")
        result[kid]=jwt.PyJWK.from_dict(key,algorithm="EdDSA").key
    return result


def signing_key(token):
    cfg=configuration()
    if cfg is None: raise NeonAuthError("Neon authentication is not configured")
    header=jwt.get_unverified_header(token)
    kid=header.get("kid")
    if header.get("alg")!="EdDSA" or not isinstance(kid,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",kid):
        raise NeonAuthError("Invalid Neon token header")
    with _cache_lock:
        now=time.monotonic(); entry=_cache.get(cfg["jwks"])
        refresh=entry is None or now-entry["fetched"] >= 300
        if entry and kid not in entry["keys"] and now-entry["fetched"] >= 30: refresh=True
        if refresh:
            if now-_attempts.get(cfg["jwks"],float("-inf"))<30:
                raise NeonAuthError("Neon signing keys temporarily unavailable")
            _attempts[cfg["jwks"]]=now
            try:
                keys=_fetch_keys(cfg["jwks"])
            except (httpx.HTTPError,OSError,ValueError,TypeError,KeyError):
                raise NeonAuthError("Neon signing keys unavailable") from None
            entry={"keys":keys,"fetched":now}
            _cache.clear();_cache[cfg["jwks"]]=entry
        if kid not in entry["keys"]: raise NeonAuthError("Unknown Neon signing key")
        return entry["keys"][kid]


def decode_token(token):
    cfg=configuration()
    claims=jwt.decode(token,signing_key(token),algorithms=["EdDSA"],issuer=cfg["origin"],audience=cfg["origin"],leeway=0,
                      options={"require":["iss","sub","aud","exp","iat"],"verify_iat":True})
    if claims.get("iss")!=cfg["origin"] or claims.get("aud")!=cfg["origin"]:
        raise NeonAuthError("Neon token audience or issuer differs")
    return claims


def _proxy_target(path,query):
    cfg=configuration()
    decoded=unquote(path)
    if (cfg is None or len(path)>1200 or len(query)>2048 or "\\" in decoded or any(part in {".",".."} for part in decoded.split("/"))
            or not re.fullmatch(r"[A-Za-z0-9_./-]*",decoded) or decoded.startswith("/") or "//" in decoded):
        raise NeonAuthError("Invalid authentication route")
    return cfg,cfg["base"]+("/"+path if path else "")+("?"+query if query else "")


GET_PATHS={"get-session","token","jwks",".well-known/jwks.json","list-accounts","verify-email"}
POST_PATHS={"email-otp/verify-email","email-otp/send-verification-otp","email-otp/request-password-reset","email-otp/reset-password","sign-in/email-otp","sign-in/email","sign-up/email","sign-out","get-session","token","request-password-reset","forget-password","reset-password","send-verification-email","verify-email","update-user","change-password"}


def _safe_redirect(value,origins):
    if not isinstance(value,str) or len(value)>2048:return False
    decoded=unquote(value)
    if "\\" in decoded or any(ord(c)<32 or ord(c)==127 for c in decoded) or decoded.startswith("//"):return False
    parsed=urlsplit(decoded)
    if parsed.username or parsed.password:return False
    if parsed.scheme or parsed.netloc:return parsed.scheme+"://"+parsed.netloc in origins
    return decoded.startswith("/") and not decoded.startswith("//")


def _authentication_cookies(raw):
    """Do not forward deployment-protection or unrelated application cookies."""
    parsed=SimpleCookie();filtered=SimpleCookie()
    try:parsed.load(raw)
    except CookieError:return ""
    for name,morsel in parsed.items():
        base=name.removeprefix("__Secure-").removeprefix("__Host-")
        if base.startswith(("better-auth.","neon-auth.")):
            filtered[name]=morsel.value
    return filtered.output(header="",sep=";").strip()


async def proxy(request,path,settings):
    from fastapi.responses import Response
    cfg,target=_proxy_target(path,request.url.query)
    reset_link=request.method=="GET" and re.fullmatch(r"reset-password/[A-Za-z0-9_.-]{1,1024}",path)
    if not reset_link and path not in (GET_PATHS if request.method=="GET" else POST_PATHS if request.method=="POST" else GET_PATHS|POST_PATHS):
        return Response(status_code=404)
    if len(request.headers.get("cookie",""))>16384:return Response(status_code=413)
    if request.method not in {"GET","POST","OPTIONS"}: raise NeonAuthError("Unsupported authentication method")
    origin=request.headers.get("origin")
    if request.method=="POST" and (not origin or origin.rstrip("/") not in settings.allowed_origins):
        return Response(status_code=403)
    body=await request.body()
    if len(body)>MAX_PROXY_REQUEST: return Response(status_code=413)
    if body:
        try: value=json.loads(body)
        except (ValueError,UnicodeError): return Response(status_code=422)
        if isinstance(value,dict):
            for name in ("callbackURL","errorCallbackURL","newUserCallbackURL","redirectTo"):
                url=value.get(name)
                if url is not None and not _safe_redirect(url,settings.allowed_origins):
                    return Response(status_code=403)
    headers={name:request.headers[name] for name in ("cookie","content-type","accept","origin","referer","x-force-fetch") if name in request.headers}
    headers.pop("cookie",None)
    cookie=_authentication_cookies(request.headers.get("cookie",""))
    if cookie:headers["cookie"]=cookie
    headers["Accept-Encoding"]="identity"
    async with httpx.AsyncClient(timeout=10,trust_env=False,follow_redirects=False) as client:
        async with client.stream(request.method,target,content=body,headers=headers) as upstream:
            if upstream.headers.get("content-encoding","identity") not in {"identity",""}: return Response(status_code=502)
            content=bytearray()
            async for chunk in upstream.aiter_bytes():
                if len(chunk)>MAX_PROXY_RESPONSE-len(content): return Response(status_code=502)
                content.extend(chunk)
            response=Response(bytes(content),status_code=upstream.status_code,
                              headers={name:upstream.headers[name] for name in ("content-type","set-auth-jwt") if name in upstream.headers})
            location=upstream.headers.get("location")
            if location:
                if location==cfg["base"] or location.startswith(cfg["base"]+"/"): location="/auth"+location[len(cfg["base"]):]
                if not _safe_redirect(location,settings.allowed_origins):return Response(status_code=502)
                response.headers["Location"]=location
            if len(upstream.headers.get("set-auth-jwt",""))>16384:return Response(status_code=502)
            cookies=upstream.headers.get_list("set-cookie")
            if len(cookies)>16: return Response(status_code=502)
            for raw in cookies:
                if len(raw)>8192: return Response(status_code=502)
                cookie=SimpleCookie();cookie.load(raw)
                for name,morsel in cookie.items():
                    morsel["domain"]="";morsel["path"]="/" if name.startswith("__Host-") else "/auth"
                    if settings.app_env=="production":morsel["secure"]=True
                    response.headers.append("Set-Cookie",morsel.OutputString())
            return response
