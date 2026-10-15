"""Optional authenticated API; no default credentials or executable dynamic config."""
import hmac
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from starlette.responses import JSONResponse
from fastapi import FastAPI, Header, HTTPException, Request
from .adapter import Bridge, BridgeError, Principal

def create_app(bridge: Bridge, api_keys: dict[str, Principal]):
    if not api_keys or any(not isinstance(key,str) or len(key)<16 for key in api_keys):
        raise ValueError("host_scoped_credentials_required")
    app=FastAPI(title="Gradia Ruflo local bridge",version="0.1.0")
    attempts=defaultdict(lambda:deque(maxlen=100))
    private_attempts=deque(maxlen=100)
    @app.middleware("http")
    async def metadata(request, call_next):
        started=time.monotonic();correlation=str(uuid.uuid4())
        request.state.correlation=correlation
        try: response=await call_next(request)
        except Exception: response=JSONResponse({"detail":"host_execution_failure"},status_code=503)
        try: identity=principal(request.headers.get("authorization"))
        except HTTPException: identity=None
        route=getattr(request.scope.get("route"),"path","")
        operation={"/v1/providers/{provider}/catalog":"bridge.catalog","/v1/providers/{provider}/execute":"bridge.execute","/v1/providers/{provider}/diagnostics":"bridge.diagnostics"}.get(route,"bridge.unknown")
        event={"schema":"gradia.bridge.attempt.v1","correlation_id":correlation,"operation":operation,"result":"refused" if response.status_code>=400 else "returned","http_status":response.status_code,"occurred_at":datetime.now(timezone.utc).isoformat(),"duration_ms":round((time.monotonic()-started)*1000,3),"cost":{"status":"unknown","amount":None}}
        if identity and identity.tenant_id in bridge.workspaces: attempts[identity.tenant_id].append(event)
        else: private_attempts.append(event)
        response.headers["X-Gradia-Correlation-Id"]=correlation
        response.headers["X-Gradia-Handler-Duration-Ms"]=str(round((time.monotonic()-started)*1000,3))
        response.headers["X-Gradia-Cost-Status"]="unknown"
        response.headers["Cache-Control"]="no-store"
        return response
    def principal(authorization):
        value=(authorization or "").removeprefix("Bearer ")
        if len(value)>1024: raise HTTPException(401,detail="unauthorized")
        for key,identity in api_keys.items():
            if authorization and authorization.startswith("Bearer ") and hmac.compare_digest(value.encode("utf-8"),key.encode("utf-8")): return identity
        raise HTTPException(401,detail="unauthorized")
    @app.get("/v1/providers/{provider}/diagnostics")
    def diagnostics(provider:str,authorization:str|None=Header(None)):
        identity=principal(authorization)
        if identity.tenant_id not in bridge.workspaces or f"{provider}:inspect" not in identity.permissions: raise HTTPException(403,detail="permission_denied")
        return {"events":list(attempts[identity.tenant_id]),"coverage":"volatile last100 attempts per tenant; older records evicted; unauthenticated attempts remain private; no central collector"}
    @app.get("/v1/providers/{provider}/catalog")
    def catalog(provider:str,edition:str="3.56.9",authorization:str|None=Header(None)):
        identity=principal(authorization)
        if identity.tenant_id not in bridge.workspaces or f"{provider}:inspect" not in identity.permissions: raise HTTPException(403,detail="permission_denied")
        try: return bridge.catalog(edition,provider)
        except BridgeError as error: raise HTTPException(error.status,detail=error.code)
    @app.post("/v1/providers/{provider}/execute")
    async def execute(provider:str,request:Request,authorization:str|None=Header(None),idempotency_key:str|None=Header(None)):
        identity=principal(authorization)
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>4096: raise HTTPException(413,detail="request_too_large")
            raw.extend(chunk)
        try:
            import json
            def unique(pairs):
                result={}
                for key,value in pairs:
                    if key in result: raise ValueError("duplicate_json_key")
                    result[key]=value
                return result
            value=json.loads(raw,object_pairs_hook=unique)
            # Run blocking CLI work outside the event loop; Bridge owns global concurrency.
            from starlette.concurrency import run_in_threadpool
            return await run_in_threadpool(bridge.execute,identity,value,idempotency_key,request.state.correlation,provider)
        except BridgeError as error: raise HTTPException(error.status,detail=error.code)
        except (ValueError,UnicodeDecodeError,RecursionError): raise HTTPException(400,detail="invalid_json")
    return app
