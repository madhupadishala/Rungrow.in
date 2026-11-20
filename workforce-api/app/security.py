from datetime import datetime,timedelta,timezone
import jwt
from fastapi import Depends,HTTPException
from fastapi.security import HTTPAuthorizationCredentials,HTTPBearer
from pwdlib import PasswordHash
from .config import settings
from .policy import Principal
bearer=HTTPBearer(auto_error=False)
password_hash=PasswordHash.recommended()
def hash_password(password:str)->str:
    if len(password)<12: raise ValueError("Password must contain at least 12 characters")
    return password_hash.hash(password)
def verify_password(password:str,hashed:str)->bool: return password_hash.verify(password,hashed)
def issue_access_token(user_id:str,tenant_id:str,role:str,permissions:list[str])->str:
    now=datetime.now(timezone.utc)
    return jwt.encode({"sub":user_id,"tid":tenant_id,"role":role,"perms":permissions,"iss":settings.jwt_issuer,"iat":now,"exp":now+timedelta(minutes=settings.access_token_minutes)},settings.jwt_secret,algorithm="HS256")
async def current_principal(credentials:HTTPAuthorizationCredentials|None=Depends(bearer))->Principal:
    if credentials is None: raise HTTPException(401,"Bearer token required",headers={"WWW-Authenticate":"Bearer"})
    try:
        if settings.app_env == "production":
            if not settings.jwt_public_key: raise HTTPException(503,"OIDC verification key is not configured")
            data=jwt.decode(credentials.credentials,settings.jwt_public_key,algorithms=["RS256"],issuer=settings.jwt_issuer,audience=settings.jwt_audience,options={"require":["sub","tid","role","exp","iss","aud"]})
        else:
            data=jwt.decode(credentials.credentials,settings.jwt_secret,algorithms=["HS256"],issuer=settings.jwt_issuer,options={"require":["sub","tid","role","exp","iss"]})
        return Principal(data["sub"],data["tid"],data["role"],frozenset(data.get("perms",[])))
    except jwt.PyJWTError as exc: raise HTTPException(401,"Invalid or expired access token") from exc
def require_permission(permission:str):
    async def guard(principal:Principal=Depends(current_principal)):
        if permission not in principal.permissions and "*" not in principal.permissions: raise HTTPException(403,"Permission denied")
        return principal
    return guard
