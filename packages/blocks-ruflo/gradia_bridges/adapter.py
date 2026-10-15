from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from typing import Callable, Protocol
import re
import math
from datetime import datetime, timezone
from contextlib import contextmanager

MAX_OUTPUT = 65536

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),allow_nan=False)

class BridgeError(ValueError):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)

@dataclass(frozen=True)
class Principal:
    tenant_id: str
    permissions: frozenset[str]

def validate_arguments(schema: dict, arguments: dict | None = None):
    if not isinstance(schema,dict) or len(schema)>16: raise BridgeError("invalid_host_schema",503)
    for name,definition in schema.items():
        if not isinstance(name,str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}",name) or not isinstance(definition,dict): raise BridgeError("invalid_host_schema",503)
        if set(definition)-{"type","enum","minimum","maximum","minLength","maxLength","optional"}: raise BridgeError("invalid_host_schema",503)
        kind=definition.get("type")
        if kind not in (None,"string","integer","number","boolean") or kind is None and "enum" not in definition: raise BridgeError("invalid_host_schema",503)
        if "optional" in definition and type(definition["optional"]) is not bool: raise BridgeError("invalid_host_schema",503)
        if "enum" in definition and (not isinstance(definition["enum"],list) or not 1<=len(definition["enum"])<=32 or any(type(item) not in (str,int,float,bool) or isinstance(item,float) and not math.isfinite(item) for item in definition["enum"])): raise BridgeError("invalid_host_schema",503)
        for bound in ("minimum","maximum"):
            if bound in definition and (type(definition[bound]) not in (int,float) or not math.isfinite(definition[bound]) or kind not in ("integer","number")): raise BridgeError("invalid_host_schema",503)
        for bound in ("minLength","maxLength"):
            if bound in definition and (type(definition[bound]) is not int or not 0<=definition[bound]<=4096 or kind!="string"): raise BridgeError("invalid_host_schema",503)
    if arguments is None: return
    if not isinstance(arguments,dict) or set(arguments)-set(schema) or any(name not in arguments and not definition.get("optional",False) for name,definition in schema.items()): raise BridgeError("invalid_arguments")
    for name,value in arguments.items():
        definition=schema[name];kind=definition.get("type")
        if kind=="string" and not isinstance(value,str) or kind=="integer" and type(value) is not int or kind=="boolean" and type(value) is not bool or kind=="number" and (type(value) not in (int,float) or not math.isfinite(value)): raise BridgeError("invalid_arguments")
        if "enum" in definition and not any(type(value) is type(item) and value==item for item in definition["enum"]): raise BridgeError("invalid_arguments")
        if "minimum" in definition and value<definition["minimum"] or "maximum" in definition and value>definition["maximum"]: raise BridgeError("invalid_arguments")
        if "minLength" in definition and len(value)<definition["minLength"] or "maxLength" in definition and len(value)>definition["maxLength"]: raise BridgeError("invalid_arguments")

@dataclass(frozen=True)
class OperationSpec:
    id: str
    permission: str
    effect: str
    arguments: dict
    build_argv: Callable[[dict], list[str]]

def _empty(argv):
    def build(arguments):
        if arguments: raise BridgeError("invalid_arguments")
        return list(argv)
    return build

def _init(arguments):
    if set(arguments) != {"topology", "max_agents"} or arguments["topology"] not in ("hierarchical", "mesh", "ring", "star") or type(arguments["max_agents"]) is not int or not 1 <= arguments["max_agents"] <= 4:
        raise BridgeError("invalid_arguments")
    return ["swarm", "init", "--topology", arguments["topology"], "--max-agents", str(arguments["max_agents"]), "--format", "json"]

DEFAULT_OPERATIONS = (
    OperationSpec("system.version", "ruflo:inspect", "inspect", {}, _empty(["--version"])),
    OperationSpec("swarm.status", "ruflo:inspect", "inspect", {}, _empty(["swarm", "status", "--format", "json"])),
    OperationSpec("swarm.init", "ruflo:swarm:init", "mutate", {"topology":{"enum":["hierarchical","mesh","ring","star"]},"max_agents":{"type":"integer","minimum":1,"maximum":4}}, _init),
)

class ProviderEdition(Protocol):
    node: Path
    entry: Path
    provider_id: str
    expected_version: str
    def contract(self) -> dict: ...
    def verify(self) -> None: ...
    def specs(self) -> dict[str, OperationSpec]: ...
    def argv(self, operation: str, arguments: dict) -> list[str]: ...

@dataclass(frozen=True)
class LocalCliEdition:
    node: Path
    entry: Path
    manifest: Path
    operations: tuple[OperationSpec, ...] = ()
    provider_id: str = ""
    expected_version: str = ""
    expected_package: str = ""
    manifest_sha256: str = field(init=False, repr=False)
    def __post_init__(self):
        if self.manifest.stat().st_size > 65536: raise BridgeError("contract_too_large",503)
        object.__setattr__(self,"manifest_sha256",sha(self.manifest.read_bytes()))
    def specs(self):
        specs={operation.id:operation for operation in self.operations}
        if len(specs)!=len(self.operations) or any(operation.effect not in ("inspect","mutate") or not operation.permission for operation in self.operations):
            raise BridgeError("invalid_host_registry",503)
        for operation in self.operations: validate_arguments(operation.arguments)
        return specs
    def contract(self):
        raw=self.manifest.read_bytes()
        if len(raw)>65536 or sha(raw)!=self.manifest_sha256:
            raise BridgeError("contract_authority_or_identity_mismatch",503)
        value = json.loads(raw)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}",self.provider_id) or not self.expected_version or not self.expected_package or value.get("id") != self.provider_id or value.get("edition") != self.expected_version or value.get("upstream",{}).get("package") != self.expected_package or value.get("upstream",{}).get("version") != self.expected_version:
            raise BridgeError("unregistered_edition", 503)
        specs=self.specs();declared=value.get("operations",[])
        if not isinstance(declared,list) or len(declared)!=len(specs): raise BridgeError("unregistered_contract",503)
        seen=set()
        for descriptor in declared:
            if not isinstance(descriptor,dict) or descriptor.get("id") not in specs or descriptor["id"] in seen: raise BridgeError("unregistered_contract",503)
            spec=specs[descriptor["id"]];seen.add(spec.id)
            if any(descriptor.get(key)!=getattr(spec,key) for key in ("permission","effect","arguments")):
                raise BridgeError("contract_authority_mismatch",503)
        return value
    def verify(self):
        contract = self.contract()
        if not self.node.is_absolute() or not self.entry.is_absolute() or not self.node.is_file() or not self.entry.is_file():
            raise BridgeError("runtime_unavailable", 503)
        try:
            package = json.loads((self.entry.parent.parent / "package.json").read_text())
            valid=package.get("name") == self.expected_package and package.get("version") == contract["edition"] and sha(self.entry.read_bytes()) == contract["upstream"]["entrySha256"]
        except (OSError,ValueError,KeyError,TypeError): valid=False
        if not valid: raise BridgeError("runtime_identity_mismatch",503)
    def argv(self, operation: str, arguments: dict):
        spec=self.specs().get(operation)
        if spec is None: raise BridgeError("operation_not_registered",404)
        validate_arguments(spec.arguments,arguments)
        argv=spec.build_argv(arguments)
        if not isinstance(argv,list) or len(argv)>32 or any(not isinstance(value,str) or len(value)>256 or "\x00" in value for value in argv): raise BridgeError("invalid_host_argv",503)
        return argv

@dataclass(frozen=True)
class RufloEdition(LocalCliEdition):
    operations: tuple[OperationSpec, ...] = DEFAULT_OPERATIONS
    provider_id: str = "ruflo"
    expected_version: str = "3.56.9"
    expected_package: str = "@claude-flow/cli"

class Registry:
    """Only application code registers reviewed editions; no dynamic executable config."""
    def __init__(self, editions: list[ProviderEdition]):
        self.editions = {}
        for edition in editions:
            contract=edition.contract()
            key = (contract["id"],contract["edition"])
            if key in self.editions:
                raise BridgeError("duplicate_edition")
            self.editions[key] = edition
    def get(self, edition: str, provider: str = "ruflo"):
        key=(provider,edition)
        if key not in self.editions:
            raise BridgeError("edition_not_registered", 409)
        return self.editions[key]

class Bridge:
    def __init__(self, registry: Registry, workspaces: dict[str, Path], journal: Path, *, timeout: float = 15):
        if not 0 < timeout <= 20:
            raise BridgeError("invalid_timeout")
        self.registry, self.timeout = registry, timeout
        self.workspaces = {}
        for tenant, path in workspaces.items():
            path = Path(path)
            if not path.is_absolute() or not path.is_dir() or path.is_symlink():
                raise BridgeError("invalid_workspace")
            resolved = path.resolve()
            if any(resolved == prior or resolved.is_relative_to(prior) or prior.is_relative_to(resolved) for prior in self.workspaces.values()):
                raise BridgeError("workspace_overlap")
            self.workspaces[tenant] = resolved
        journal = Path(journal)
        if journal.is_symlink() or (journal.exists() and journal.stat().st_mode & 0o077):
            raise BridgeError("unsafe_journal")
        if not journal.exists():
            fd = os.open(journal, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
        self.journal = journal
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS executions (tenant TEXT,key TEXT,request_hash TEXT,receipt TEXT,PRIMARY KEY(tenant,key))")
        self.slots = threading.BoundedSemaphore(2)
    @contextmanager
    def _db(self):
        connection=sqlite3.connect(self.journal,timeout=5)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    def catalog(self, edition: str, provider: str = "ruflo"):
        contract = self.registry.get(edition,provider).contract()
        return {"contract": contract, "contractSha256": sha(canonical(contract).encode())}
    def execute(self, principal: Principal, request: dict, idempotency_key: str | None = None, correlation_id: str | None = None, provider: str = "ruflo"):
        correlation_id=correlation_id or str(uuid.uuid4())
        if principal.tenant_id not in self.workspaces:
            raise BridgeError("tenant_not_configured", 403)
        if not isinstance(request, dict) or set(request) != {"edition", "operation", "arguments", "expected_contract_hash"} or not isinstance(request["arguments"], dict):
            raise BridgeError("invalid_request")
        try: request_bytes=canonical(request).encode()
        except (ValueError,TypeError,RecursionError): raise BridgeError("invalid_request")
        if len(request_bytes)>4096: raise BridgeError("request_too_large",413)
        if not all(isinstance(request[key], str) and len(request[key]) <= 128 for key in ("edition", "operation", "expected_contract_hash")):
            raise BridgeError("invalid_request")
        edition = self.registry.get(request["edition"],provider)
        catalog = self.catalog(request["edition"],provider)
        if request["expected_contract_hash"] != catalog["contractSha256"]:
            raise BridgeError("contract_identity_mismatch", 409)
        operation = next((item for item in catalog["contract"]["operations"] if item["id"] == request["operation"]), None)
        if operation is None:
            raise BridgeError("operation_not_registered", 404)
        spec=edition.specs()[request["operation"]]
        if spec.permission not in principal.permissions:
            raise BridgeError("permission_denied", 403)
        argv = edition.argv(request["operation"], request["arguments"])
        mutate = spec.effect == "mutate"
        fingerprint = sha(canonical({"provider":provider,"request":request}).encode())
        if mutate:
            if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128 or not idempotency_key.isascii():
                raise BridgeError("idempotency_key_required")
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT request_hash,receipt FROM executions WHERE tenant=? AND key=?", (principal.tenant_id, idempotency_key)).fetchone()
                if row:
                    if row[0] != fingerprint: raise BridgeError("idempotency_conflict", 409)
                    return json.loads(row[1]) if row[1] else {"result":"uncertain","reason":"execution_in_progress_or_interrupted","retry_safe":False}
                db.execute("INSERT INTO executions VALUES (?,?,?,NULL)", (principal.tenant_id, idempotency_key, fingerprint))
        if not self.slots.acquire(blocking=False):
            # A reserved mutation intentionally remains uncertain; never risk an automatic retry.
            raise BridgeError("capacity_exhausted", 429)
        started = time.monotonic()
        try:
            edition.verify()
            workspace = self.workspaces[principal.tenant_id]
            home = workspace / f".gradia-{provider}-home"
            if home.is_symlink() or (home.exists() and home.stat().st_mode & 0o077): raise BridgeError("unsafe_runtime_home", 503)
            home.mkdir(mode=0o700, exist_ok=True)
            env = {"HOME":str(home),"PATH":str(edition.node.parent),"RUFLO_DAEMON_AUTOSTART":"0","NO_COLOR":"1","TERM":"dumb","CI":"1"}
            outcome, exit_code, output = _run([str(edition.node),str(edition.entry),*argv], workspace, env, self.timeout)
            receipt = {"schema":"gradia.bridge.execution.v1","event_id":str(uuid.uuid4()),"correlation_id":correlation_id,"occurred_at":datetime.now(timezone.utc).isoformat(),"tenant_id":principal.tenant_id,"provider":provider,"edition":request["edition"],"operation":request["operation"],"contract_sha256":catalog["contractSha256"],"result":"completed" if outcome == "exited" and exit_code == 0 else ("uncertain" if mutate else "failed"),"process_outcome":outcome,"exit_code":exit_code,"duration_ms":round((time.monotonic()-started)*1000,3),"output_bytes":len(output),"output_sha256":sha(output),"cost":{"status":"unknown","amount":None},"retry_safe":not mutate,"boundary":"CLI exit is not workflow or model-backed task success; raw logs withheld"}
            if provider == "ruflo" and request["operation"] == "swarm.status" and exit_code == 0:
                try:
                    parsed=json.loads(output)
                    if not isinstance(parsed,dict): raise ValueError("not_object")
                    safe={}
                    for key in ("id","topology","status"):
                        if parsed.get(key) is None or isinstance(parsed.get(key),str) and len(parsed[key])<=128: safe[key]=parsed.get(key)
                    if isinstance(parsed.get("hasActiveSwarm"),bool): safe["hasActiveSwarm"]=parsed["hasActiveSwarm"]
                    for group in ("agents","tasks","coordination"):
                        values=parsed.get(group,{})
                        if isinstance(values,dict):
                            allowed={"agents":("total","active","idle","completed"),"tasks":("total","completed","inProgress","pending"),"coordination":("consensusRounds","messagesSent","conflictsResolved")}[group]
                            safe[group]={key:values[key] for key in allowed if type(values.get(key)) is int and 0<=values[key]<=1000000}
                    receipt["data"]=safe;receipt["structured_output_status"]="selected_fields_only"
                except (ValueError,UnicodeDecodeError): receipt["structured_output_status"]="unavailable"
            if request["operation"] == "system.version" and exit_code == 0:
                receipt["version"] = catalog["contract"]["edition"]
            if mutate:
                with self._db() as db: db.execute("UPDATE executions SET receipt=? WHERE tenant=? AND key=?",(canonical(receipt),principal.tenant_id,idempotency_key))
            return receipt
        except Exception:
            if mutate:
                receipt={"schema":"gradia.bridge.execution.v1","event_id":str(uuid.uuid4()),"correlation_id":correlation_id,"occurred_at":datetime.now(timezone.utc).isoformat(),"tenant_id":principal.tenant_id,"provider":provider,"edition":request["edition"],"operation":request["operation"],"result":"uncertain","reason":"host_or_runtime_failure_after_reservation","retry_safe":False,"cost":{"status":"unknown","amount":None}}
                try:
                    with self._db() as db: db.execute("UPDATE executions SET receipt=? WHERE tenant=? AND key=?",(canonical(receipt),principal.tenant_id,idempotency_key))
                except sqlite3.Error: pass
                return receipt
            raise
        finally:
            self.slots.release()

def _run(argv, cwd, env, timeout):
    process = subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=False,start_new_session=True)
    selector = selectors.DefaultSelector()
    for stream in (process.stdout,process.stderr):
        os.set_blocking(stream.fileno(),False); selector.register(stream,selectors.EVENT_READ)
    data = bytearray(); deadline=time.monotonic()+timeout; outcome="exited"
    try:
        while selector.get_map():
            if time.monotonic() >= deadline: outcome="timeout"; break
            for key,_ in selector.select(min(.1,max(0,deadline-time.monotonic()))):
                chunk=os.read(key.fileobj.fileno(),8192)
                if not chunk: selector.unregister(key.fileobj); continue
                room=MAX_OUTPUT-len(data); data.extend(chunk[:room])
                if len(chunk)>room: outcome="output_limit"; break
            if outcome!="exited": break
    finally:
        # Cleanup the whole original process group even when the parent exited first.
        if outcome == "exited":
            try: process.wait(timeout=.2)
            except subprocess.TimeoutExpired: outcome="closed_output_before_exit"
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        except PermissionError:
            if process.poll() is None:
                process.kill()
                outcome="cleanup_unverified"
        process.wait(timeout=2)
        selector.close(); process.stdout.close(); process.stderr.close()
    return outcome, process.returncode, bytes(data)
