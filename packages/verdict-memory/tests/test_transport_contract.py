"""Actual ai-memory 2.6.6 MCP shapes; no live network or server-only fake fields."""
import json
from dataclasses import replace

import httpx
import pytest

from verdict_memory.client import AiMemoryClient, AiMemoryClientConfig
from verdict_memory.contracts import (
    EventIdentity, ExternalMemoryRef, LifecycleEvent, MemoryProviderError,
    ProviderErrorCode, ProviderResultStatus, RecallQuery,
)
from verdict_memory.outbox import MemoryOutbox
from verdict_memory.mirror import MemoryMirrorWorker


def event(**kwargs):
    base = dict(kind="after_edit", content="safe marker", created_at=1.0,
                identity=EventIdentity("prime", "allowed", "session", source_event_id="source"))
    base.update(kwargs)
    return LifecycleEvent(**base)


class Server:
    """Stateful HTTP MCP fixture implementing only documented tool fields."""
    def __init__(self):
        self.pages = {}
        self.calls = []
        self.write_ack = None
        self.read_transform = lambda x: x
        self.delete_ack = None
        self.query_result = None
        self.outage = False
        self.malformed = None

    def handle(self, request):
        if self.outage:
            raise httpx.ConnectError("offline", request=request)
        rpc = json.loads(request.content)
        if rpc["method"] == "notifications/initialized":
            return httpx.Response(202)
        if rpc["method"] == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "ai-memory", "version": "2.6.6"}}
        else:
            name, args = rpc["params"]["name"], rpc["params"]["arguments"]
            self.calls.append((name, args))
            if self.malformed is not None:
                return httpx.Response(200, json=self.malformed)
            path = args.get("path")
            if name == "memory_write_page":
                assert set(args) <= {"path", "body", "workspace", "project", "kind"}
                self.pages[path] = {"path": path, "title": "Verdict memory event",
                                    "body": args["body"], "frontmatter": {"kind": args["kind"]}}
                data = self.write_ack if self.write_ack is not None else {"path": path, "page_id": "page-uuid", "checkpoint": None}
            elif name == "memory_read_page":
                if path not in self.pages:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"], "error": {
                        "code": -32603, "message": f"page {path} not found in resolved scope default/allowed"}})
                data = self.read_transform(dict(self.pages[path]))
            elif name == "memory_query":
                assert args["answer"] is False
                data = self.query_result if self.query_result is not None else {
                    "hits": [{"path": p, "rank": 1.0, "snippet": "<mark>safe</mark>"} for p in self.pages],
                    "raw_hits": [], "global_scope_hits": []}
            elif name == "memory_delete_page":
                self.pages.pop(path, None)
                data = self.delete_ack if self.delete_ack is not None else {"path": path, "deleted": True, "checkpoint": None}
            else:
                raise AssertionError(name)
            result = {"content": [{"type": "text", "text": json.dumps(data)}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"], "result": result})

    def client(self):
        return AiMemoryClient(AiMemoryClientConfig(project="allowed", token="fake", token_file=None,
                              transport=httpx.MockTransport(self.handle)))

    @property
    def writes(self):
        return [args for name, args in self.calls if name == "memory_write_page"]


def test_persisted_envelope_and_exact_readback_before_ack():
    s = Server(); c = s.client(); e = event()
    ref = c.put(e)
    assert ref.revision == e.digest()
    assert e.digest() in s.writes[0]["body"]
    assert e.idempotency_key in s.writes[0]["body"]
    assert e.identity.source_event_id in s.writes[0]["body"]
    assert [n for n, _ in s.calls] == ["memory_read_page", "memory_write_page", "memory_read_page"]
    hit = c.search(RecallQuery("safe", project="allowed")).hits[0]
    assert hit.event.to_dict() == e.to_dict()
    assert hit.ref == ref and hit.untrusted


def test_direct_replay_and_conflict_even_with_changed_kind():
    s = Server(); c = s.client(); e = event()
    first = c.put(e)
    assert s.client().put(replace(e, created_at=99.0, observed_at=100.0)) == first
    for changed in (replace(e, content="changed", content_hash=""), replace(e, memory_kind="other")):
        with pytest.raises(MemoryProviderError) as err:
            s.client().put(changed)
        assert err.value.code.value == "conflict"
    assert len(s.writes) == 1


@pytest.mark.parametrize("change", [
    lambda p: {**p, "path": "elsewhere/" + p["path"].split("/")[-1]},
    lambda p: {**p, "body": p["body"].replace("safe marker", "tampered")},
    lambda p: {**p, "body": p["body"].replace('"event_digest":"', '"event_digest":"wrong')},
    lambda p: {**p, "body": "{}"},
])
def test_corrupt_readback_never_mirror_acks(tmp_path, change):
    s = Server(); s.read_transform = change
    box = MemoryOutbox(tmp_path / "o.db"); box.enqueue(event())
    result = MemoryMirrorWorker(box, s.client()).run_once()
    assert result.acked == 0 and box.count("acked") == 0
    assert box.count("pending") == 1


@pytest.mark.parametrize("ack", [{}, {"path": "other.md"}, {"path": "prefix/" + "x" * 64 + ".md"}])
def test_write_ack_must_name_exact_object(ack):
    s = Server(); s.write_ack = ack
    with pytest.raises(MemoryProviderError):
        s.client().put(event())


def test_private_and_tenant_scope_not_relabelled():
    s = Server(); c = s.client()
    c.put(event(scope="private-b", identity=EventIdentity("prime", "allowed", "s", source_event_id="s", tenant="tenant-b")))
    for query in (RecallQuery("safe", project="allowed", scope="private-a", tenant="tenant-b"),
                  RecallQuery("safe", project="allowed", scope="private-b", tenant="tenant-a")):
        result = c.search(query)
        assert result.status is ProviderResultStatus.AVAILABLE and result.hits == ()
    result = c.search(RecallQuery("safe", project="allowed", scope="private-b", tenant="tenant-b"))
    assert len(result.hits) == 1
    assert result.hits[0].event.scope == "private-b"


@pytest.mark.parametrize("path", ["verdict-memory/../operator.md", "verdict-memory/events/../a.md", "verdict-memory/events/" + "a"*64 + ".md/extra", "/verdict-memory/events/" + "a"*64 + ".md"])
def test_delete_rejects_noncanonical_paths_before_rpc(path):
    s = Server()
    with pytest.raises(MemoryProviderError):
        s.client().delete(ExternalMemoryRef("ai-memory", path))
    assert s.calls == []


@pytest.mark.parametrize("ack", [{}, {"unrelated": True}, {"path": "foreign.md", "deleted": True}, {"deleted": "true"}])
def test_delete_semantic_ack_and_pre_read(ack):
    s = Server(); c = s.client(); ref = c.put(event()); s.delete_ack = ack
    assert c.delete(ref) is False


def test_delete_verified_object_and_absence():
    s = Server(); c = s.client(); ref = c.put(event())
    assert c.delete(ref) is True
    assert c.delete(ref) is False
    assert [n for n, _ in s.calls][-3:] == ["memory_delete_page", "memory_read_page", "memory_read_page"]


def test_nested_secrets_never_remote_or_outbox_and_replay_consistent(tmp_path):
    s = Server(); c = s.client()
    e = event(metadata={"nested": {"api_key": "canary-a"}}, payload={"authorization": ["canary-b"]},
              provenance={"password": {"v": "canary-c"}})
    box = MemoryOutbox(tmp_path / "o.db"); row = box.enqueue(e)
    assert row is not None
    assert box.enqueue(e).event.digest() == row.event.digest()
    assert MemoryMirrorWorker(box, c).run_once().acked == 1
    assert c.put(e).revision == row.event.digest()
    persisted = (tmp_path / "o.db").read_bytes() + s.writes[0]["body"].encode()
    for canary in (b"canary-a", b"canary-b", b"canary-c"):
        assert canary not in persisted
    assert len(s.writes) == 1


@pytest.mark.parametrize("malformed", [{}, {"jsonrpc": "2.0", "id": None, "result": {}},
    {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": '{"hits":null}'}]}},
    {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": '{"hits":[7]}'}]}}])
def test_malformed_rpc_search_not_healthy_empty(malformed):
    s = Server(); s.malformed = malformed
    result = s.client().search(RecallQuery("safe", project="allowed"))
    assert result.status is not ProviderResultStatus.AVAILABLE
    assert result.error_code is not None


def test_outage_restart_bounded_drain_conflict(tmp_path):
    s = Server(); s.outage = True
    clock = lambda: 100.0
    box = MemoryOutbox(tmp_path / "o.db", clock=clock, max_pending=2)
    box.enqueue(event()); box.enqueue(event(identity=EventIdentity("prime", "allowed", "s", source_event_id="2")))
    with pytest.raises(RuntimeError): box.enqueue(event(identity=EventIdentity("prime", "allowed", "s", source_event_id="3")))
    assert MemoryMirrorWorker(box, s.client(), clock=clock).run_once(limit=1).retried == 1
    box = MemoryOutbox(tmp_path / "o.db", clock=lambda: 102.0)
    s.outage = False
    worker = MemoryMirrorWorker(box, s.client(), clock=lambda: 102.0)
    assert worker.run_once(limit=1).acked == 1
    assert box.count("pending") == 1
    assert worker.run_once(limit=1).acked == 1
    assert worker.run_once().processed == 0
    assert len(s.writes) == 2


def test_remote_https_requires_certificate_verification():
    with pytest.raises(MemoryProviderError):
        AiMemoryClientConfig(endpoint="https://remote.example", project="allowed", verify=False)


def test_partial_write_timeout_retries_by_verified_read_without_second_write(tmp_path):
    s = Server()
    original = s.handle
    timed_out = False

    def handle(request):
        nonlocal timed_out
        response = original(request)
        rpc = json.loads(request.content)
        if rpc.get("params", {}).get("name") == "memory_write_page" and not timed_out:
            timed_out = True
            raise httpx.ReadTimeout("reply lost after commit", request=request)
        return response

    s.handle = handle
    box = MemoryOutbox(tmp_path / "o.db", clock=lambda: 100.0)
    box.enqueue(event())
    assert MemoryMirrorWorker(box, s.client(), clock=lambda: 100.0).run_once().retried == 1
    assert box.count("pending") == 1 and len(s.writes) == 1
    box = MemoryOutbox(tmp_path / "o.db", clock=lambda: 102.0)
    assert MemoryMirrorWorker(box, s.client(), clock=lambda: 102.0).run_once().acked == 1
    assert len(s.writes) == 1 and box.count("acked") == 1


def test_conflicting_remote_source_dead_letters_without_overwrite(tmp_path):
    s = Server(); s.client().put(event())
    box = MemoryOutbox(tmp_path / "o.db")
    box.enqueue(event(content="different content"))
    result = MemoryMirrorWorker(box, s.client()).run_once()
    assert result.dead_lettered == 1 and result.acked == 0
    assert box.count("dead_letter") == 1 and len(s.writes) == 1


def test_host_writer_lock_prevents_concurrent_mutation():
    s = Server(); first = s.client(); second = s.client()
    with first._writer_lock():
        with pytest.raises(MemoryProviderError) as error:
            second.put(event())
        assert error.value.code is ProviderErrorCode.DEGRADED
    assert s.calls == []
    assert second.put(event()).revision == event().digest()


@pytest.mark.parametrize("field", ["project", "workspace"])
def test_foreign_returned_scope_not_used(field):
    s = Server(); c = s.client(); c.put(event())
    path = next(iter(s.pages))
    s.query_result = {"hits": [{"path": path, field: "foreign", "rank": 1.0}],
                      "global_scope_hits": [{"path": path}], "raw_hits": [{"path": path}]}
    s.calls.clear()
    result = c.search(RecallQuery("safe", project="allowed"))
    assert result.status is ProviderResultStatus.AVAILABLE and result.hits == ()
    assert [n for n, _ in s.calls] == ["memory_query"]


def test_only_global_or_raw_hits_never_promoted():
    s = Server(); c = s.client(); c.put(event())
    path = next(iter(s.pages))
    s.query_result = {"hits": [], "global_scope_hits": [{"path": path}], "raw_hits": [{"path": path}]}
    assert c.search(RecallQuery("safe", project="allowed")).hits == ()


@pytest.mark.parametrize("response", [
    {"hits": None}, {"hits": ""}, {"hits": {}}, {"hits": [4]},
    {"hits": [{"path": None}]},
])
def test_malformed_search_shapes(response):
    s = Server(); s.query_result = response
    result = s.client().search(RecallQuery("safe", project="allowed"))
    assert result.status is ProviderResultStatus.MALFORMED


def test_denied_sensitivity_never_durable(tmp_path):
    s = Server(); e = event(sensitivity="private")
    box = MemoryOutbox(tmp_path / "o.db")
    assert box.enqueue(e) is None and box.count() == 0
    with pytest.raises(MemoryProviderError): s.client().put(e)
    assert s.calls == []


def test_delete_mismatched_revision_and_foreign_body_never_mutates():
    s = Server(); c = s.client(); ref = c.put(event())
    s.calls.clear()
    assert c.delete(replace(ref, revision="wrong")) is False
    assert all(name != "memory_delete_page" for name, _ in s.calls)
    s.pages[ref.external_id]["body"] = "# Operator page\nNot a package event"
    assert c.delete(ref) is False
    assert all(name != "memory_delete_page" for name, _ in s.calls)


def test_live_proof_compares_exact_envelope_and_cleans_only_own_ref(tmp_path, monkeypatch, capsys):
    import runpy
    import sys
    from pathlib import Path
    import verdict_memory.client as client_module
    from verdict_memory.contracts import CertificationState, ProviderHealth

    s = Server()
    real_client = AiMemoryClient
    created = []

    def factory(config):
        config.transport = httpx.MockTransport(s.handle)
        client = real_client(config)
        client.health = lambda: ProviderHealth("ai-memory", ProviderResultStatus.AVAILABLE, CertificationState.HEALTHY)
        created.append(client)
        return client

    monkeypatch.setattr(client_module, "AiMemoryClient", factory)
    script = Path(__file__).resolve().parents[1] / "scripts/live_ai_memory_proof.py"
    main = runpy.run_path(str(script))["main"]
    monkeypatch.setattr(sys, "argv", [str(script), "--allow-live-write", "--project", "isolated", "--marker", "proof"])
    assert main() == 0
    assert s.pages == {} and len(s.writes) == 1
    assert created[0].config.project.startswith("isolated-")
    output = capsys.readouterr().out
    assert "VERIFIED" in output and "CLEANUP_DELETE" in output


def test_live_proof_rejects_any_old_hit_and_still_cleans(tmp_path, monkeypatch, capsys):
    import runpy
    import sys
    from pathlib import Path
    import verdict_memory.client as client_module
    from verdict_memory.contracts import CertificationState, ProviderHealth, RecallResult

    s = Server()
    real_client = AiMemoryClient

    def factory(config):
        config.transport = httpx.MockTransport(s.handle)
        client = real_client(config)
        client.health = lambda: ProviderHealth("ai-memory", ProviderResultStatus.AVAILABLE, CertificationState.HEALTHY)
        client.search = lambda query: RecallResult(ProviderResultStatus.AVAILABLE, ())
        return client

    monkeypatch.setattr(client_module, "AiMemoryClient", factory)
    script = Path(__file__).resolve().parents[1] / "scripts/live_ai_memory_proof.py"
    main = runpy.run_path(str(script))["main"]
    monkeypatch.setattr(sys, "argv", [str(script), "--allow-live-write"])
    assert main() != 0
    assert s.pages == {} and len(s.writes) == 1


def test_redirect_never_forwards_bearer():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://evil.example/mcp"})

    client = AiMemoryClient(AiMemoryClientConfig(project="allowed", token="fake", token_file=None,
                            transport=httpx.MockTransport(handler)))
    assert client.search(RecallQuery("q", project="allowed")).status is not ProviderResultStatus.AVAILABLE
    assert len(requests) == 1 and requests[0].url.host == "127.0.0.1"


def test_malformed_health_and_outage_typed():
    client = AiMemoryClient(AiMemoryClientConfig(project="allowed", token="fake", token_file=None,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not-json"))))
    assert client.health().status is not ProviderResultStatus.AVAILABLE
    s = Server(); s.outage = True
    assert s.client().health().status is not ProviderResultStatus.AVAILABLE
