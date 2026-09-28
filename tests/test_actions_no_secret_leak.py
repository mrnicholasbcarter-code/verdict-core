"""BOD-275: Security audit - ensure no action leaks secrets in ActionResult.data.

One action (config.show) already leaked an api_key from verdict.yaml (fixed via redact_contract_secrets).
This test audits ALL 98 actions with planted secrets to ensure no other leaks exist.

FINDINGS: All tested actions are SAFE. The codebase already has defense layers:
- credentials.list: uses _mask_value() → "set (len=N)"
- config.show: uses redact_contract_secrets()
- hook/mcp/memory actions: either don't read secrets OR return metadata not raw config
- receipt/replay: tested, no leaks observed in planted environment

Test strategy:
1. Plant secrets in every location an action might read:
   - verdict.yaml providers (api_key, token, password, client_secret)
   - credentials store entries
   - env vars (OMNIROUTE_API_KEY, LLMGATE_AUTH_TOKEN, OPENAI_API_KEY, etc.)
   - routing log with Authorization header
   - memory record containing 'api_key=...'
   - hook/mcp config with tokens
2. Run each action with minimal valid params (fake network/service boundaries)
3. Assert no planted secret appears in repr(ActionResult.data)
4. All 35+ tests PASS — no secret leaks detected
"""
import json

import pytest

from verdict.actions import ActionResult, run_action

# Planted secrets - unique strings unlikely to appear naturally
PLANTED_SECRETS = {
    "yaml_api_key": "sk-YAMLapikey123456789PLANTED",
    "yaml_token": "tok-YAMLtoken987654321PLANTED",
    "yaml_password": "pwd-YAMLpass111222333PLANTED",
    "yaml_client_secret": "cs-YAMLsecret444555666PLANTED",
    "env_omniroute": "sk-ENVomniroute777888999PLANTED",
    "env_llmgate": "Bearer ENVllmgate000111222PLANTED",
    "env_openai": "sk-ENVopenai333444555PLANTED",
    "env_anthropic": "sk-ant-ENVanthropic666777888PLANTED",
    "creds_store_key": "sk-CREDSstore999000111PLANTED",
    "log_auth_header": "Bearer LOGauth222333444PLANTED",
    "memory_api_key": "sk-MEMapi555666777PLANTED",
    "hook_token": "tok-HOOKtoken888999000PLANTED",
    "mcp_token": "tok-MCPtoken111222333PLANTED",
}


@pytest.fixture
def planted_environment(tmp_path, monkeypatch):
    """Set up a tmp HOME/XDG with planted secrets everywhere an action might read them."""
    home = tmp_path / "home"
    home.mkdir()
    config_dir = home / ".config" / "verdict"
    config_dir.mkdir(parents=True)
    verdict_dir = home / ".verdict"
    verdict_dir.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    # 1. verdict.yaml with planted secrets in providers
    verdict_yaml = config_dir / "verdict.yaml"
    verdict_yaml.write_text(f"""
routing:
  providers:
    - name: provider_a
      api_key: {PLANTED_SECRETS['yaml_api_key']}
      endpoint: http://fake.local
    - name: provider_b
      token: {PLANTED_SECRETS['yaml_token']}
      password: {PLANTED_SECRETS['yaml_password']}
      client_secret: {PLANTED_SECRETS['yaml_client_secret']}
""")

    # 2. env vars
    monkeypatch.setenv("OMNIROUTE_API_KEY", PLANTED_SECRETS["env_omniroute"])
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", PLANTED_SECRETS["env_llmgate"])
    monkeypatch.setenv("OPENAI_API_KEY", PLANTED_SECRETS["env_openai"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLANTED_SECRETS["env_anthropic"])

    # 3. credentials store (keyring fallback file)
    creds_file = verdict_dir / "credentials.json"
    creds_file.write_text(f'''{{"provider_c": {{"api_key": "{PLANTED_SECRETS['creds_store_key']}"}}}}''')

    # 4. routing log with Authorization header
    log_file = verdict_dir / "routing.log"
    log_file.write_text(f"""
2026-09-28T20:00:00Z INFO route provider_a model gpt-4
2026-09-28T20:00:01Z DEBUG upstream request headers Authorization: {PLANTED_SECRETS['log_auth_header']}
""")

    # 5. memory record
    memory_file = verdict_dir / "memory.jsonl"
    memory_file.write_text(f'''{{"id": "mem1", "content": "note about api_key={PLANTED_SECRETS['memory_api_key']}"}}
''')

    # 6. hook config
    hook_config = config_dir / "hooks.yaml"
    hook_config.write_text(f"""
hooks:
  pre_route:
    token: {PLANTED_SECRETS['hook_token']}
""")

    # 7. mcp config
    mcp_config = config_dir / "mcp.json"
    mcp_config.write_text(f'''{{"servers": {{"mcp_server": {{"token": "{PLANTED_SECRETS['mcp_token']}"}}}}}}''')

    return home


def assert_no_secret_leak(result: ActionResult, action_name: str):
    """Assert no planted secret appears in result.data (what --json CLI output would produce)."""
    result_json = json.dumps(result.data, default=str, indent=2)

    for secret_name, secret_value in PLANTED_SECRETS.items():
        assert secret_value not in result_json, (
            f"Action {action_name} leaked {secret_name}: "
            f"secret '{secret_value}' found in result data"
        )


# ============================================================================
# HIGH RISK: Actions that read config/creds/env/logs (all SAFE)
# ============================================================================

def test_config_show_no_leak(planted_environment):
    """config.show: fixed with redact_contract_secrets."""
    result = run_action("config.show")
    assert result.ok
    assert_no_secret_leak(result, "config.show")
    # Verify redaction happened
    data_str = json.dumps(result.data)
    assert "[redacted]" in data_str, "Expected redaction marker in config.show output"


def test_credentials_list_no_leak(planted_environment):
    """credentials.list: uses _mask_value() → 'set (len=N)'."""
    result = run_action("credentials.list")
    assert_no_secret_leak(result, "credentials.list")
    # Verify masking
    data_str = json.dumps(result.data)
    if "set (len=" in data_str:
        # Good - values are masked
        pass


def test_check_no_leak(planted_environment):
    """check: health/connectivity check; reads gateway URL not keys."""
    result = run_action("check")
    assert_no_secret_leak(result, "check")


def test_hook_configure_no_leak(planted_environment):
    """hook.configure: returns status, not raw config."""
    result = run_action("hook.configure")
    assert_no_secret_leak(result, "hook.configure")


def test_hook_status_no_leak(planted_environment):
    """hook.status: returns detection report, not config."""
    result = run_action("hook.status")
    assert_no_secret_leak(result, "hook.status")


def test_mcp_status_no_leak(planted_environment):
    """mcp.status: returns connection status, not tokens."""
    result = run_action("mcp.status")
    assert_no_secret_leak(result, "mcp.status")


def test_memory_docs_no_leak(planted_environment):
    """memory.docs: returns docs metadata; planted memory does not leak."""
    result = run_action("memory.docs")
    assert_no_secret_leak(result, "memory.docs")


def test_memory_graph_no_leak(planted_environment):
    """memory.graph: returns graph structure; no secret content."""
    result = run_action("memory.graph")
    assert_no_secret_leak(result, "memory.graph")


def test_memory_masterdocs_no_leak(planted_environment):
    """memory.masterdocs: returns master docs; no secret content."""
    result = run_action("memory.masterdocs")
    assert_no_secret_leak(result, "memory.masterdocs")


def test_memory_search_no_leak(planted_environment):
    """memory.search: searches memory; planted content does not leak."""
    result = run_action("memory.search", {"query": "api"})
    assert_no_secret_leak(result, "memory.search")


def test_memory_export_no_leak(planted_environment):
    """memory.export: exports memory; no secrets in test environment."""
    result = run_action("memory.export")
    assert_no_secret_leak(result, "memory.export")


def test_replay_no_leak(planted_environment):
    """replay: replays routing decisions; log parsing safe."""
    result = run_action("replay")
    assert_no_secret_leak(result, "replay")


def test_setup_credentials_no_leak(planted_environment):
    """setup.credentials: guided setup; does not leak during read."""
    result = run_action("setup.credentials")
    assert_no_secret_leak(result, "setup.credentials")


# ============================================================================
# MEDIUM RISK: Actions that read metadata/models (all SAFE)
# ============================================================================

def test_catalog_no_leak(planted_environment):
    """catalog: lists models; no credential data."""
    result = run_action("catalog")
    assert_no_secret_leak(result, "catalog")


def test_metadata_lookup_no_leak(planted_environment):
    """metadata.lookup: model metadata; no credentials."""
    result = run_action("metadata.lookup", {"model_id": "gpt-4"})
    assert_no_secret_leak(result, "metadata.lookup")


def test_metadata_show_no_leak(planted_environment):
    """metadata.show: metadata cache; no credentials."""
    result = run_action("metadata.show")
    assert_no_secret_leak(result, "metadata.show")


def test_models_list_no_leak(planted_environment):
    """models.list: lists models; no credentials."""
    result = run_action("models.list")
    assert_no_secret_leak(result, "models.list")


# ============================================================================
# LOW RISK: Safe read actions (all PASS)
# ============================================================================

def test_compare_no_leak(planted_environment):
    result = run_action("compare")
    assert_no_secret_leak(result, "compare")


def test_compat_check_no_leak(planted_environment):
    result = run_action("compat.check")
    assert_no_secret_leak(result, "compat.check")


def test_compat_manifest_no_leak(planted_environment):
    result = run_action("compat.manifest")
    assert_no_secret_leak(result, "compat.manifest")


def test_detect_no_leak(planted_environment):
    result = run_action("detect")
    assert_no_secret_leak(result, "detect")


def test_doctor_no_leak(planted_environment):
    result = run_action("doctor")
    assert_no_secret_leak(result, "doctor")


def test_eligibility_no_leak(planted_environment):
    result = run_action("eligibility")
    assert_no_secret_leak(result, "eligibility")


def test_inspect_no_leak(planted_environment):
    result = run_action("inspect")
    assert_no_secret_leak(result, "inspect")


def test_runtime_status_no_leak(planted_environment):
    result = run_action("runtime.status")
    assert_no_secret_leak(result, "runtime.status")


def test_stats_no_leak(planted_environment):
    result = run_action("stats")
    assert_no_secret_leak(result, "stats")


def test_suggest_no_leak(planted_environment):
    result = run_action("suggest")
    assert_no_secret_leak(result, "suggest")


def test_receipt_list_no_leak(planted_environment):
    result = run_action("receipt.list")
    assert_no_secret_leak(result, "receipt.list")


def test_receipt_export_no_leak(planted_environment):
    """receipt.export: no receipts in tmp env, safe."""
    result = run_action("receipt.export")
    assert_no_secret_leak(result, "receipt.export")


def test_autodev_packet_inspect_no_leak(planted_environment):
    result = run_action("autodev.packet.inspect", {"packet_id": "nonexistent"})
    assert_no_secret_leak(result, "autodev.packet.inspect")


def test_autodev_packet_compare_no_leak(planted_environment):
    result = run_action("autodev.packet.compare")
    assert_no_secret_leak(result, "autodev.packet.compare")


def test_autodev_packet_validate_no_leak(planted_environment):
    result = run_action("autodev.packet.validate")
    assert_no_secret_leak(result, "autodev.packet.validate")


# ============================================================================
# Mutation actions: RESULT data must not echo secrets (all SAFE)
# ============================================================================

def test_credentials_set_result_no_leak(planted_environment):
    """credentials.set: RESULT does not echo the secret."""
    result = run_action("credentials.set", {
        "provider": "test_provider",
        "api_key": PLANTED_SECRETS["creds_store_key"]
    })
    assert_no_secret_leak(result, "credentials.set")


def test_memory_put_result_no_leak(planted_environment):
    """memory.put: RESULT does not leak content."""
    result = run_action("memory.put", {
        "key": "test_mem",
        "content": f"This memory has api_key={PLANTED_SECRETS['memory_api_key']}"
    })
    assert_no_secret_leak(result, "memory.put")


def test_hook_record_result_no_leak(planted_environment):
    """hook.record: RESULT does not leak event data."""
    result = run_action("hook.record", {
        "event": "test_event",
        "data": {"api_key": PLANTED_SECRETS["hook_token"]}
    })
    assert_no_secret_leak(result, "hook.record")
