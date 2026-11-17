# Cross-family review: BOD-413 gateway key resolver parity (PRs #1013, #1014)

Two merged PRs implement one OpenSpec change `gateway-key-resolver-parity`. Review both diffs for correctness, security (no secret leakage), and test quality. They are already merged; findings should be real defects only.

## PR #1013 — credentials.test falls through to resolver (diff below)
diff --git a/tests/test_actions_no_secret_leak.py b/tests/test_actions_no_secret_leak.py
index d40e0374..1b22ab99 100644
--- a/tests/test_actions_no_secret_leak.py
+++ b/tests/test_actions_no_secret_leak.py
@@ -395,11 +395,16 @@ def test_probe_no_leak(planted_environment):
 
 
 def test_credentials_test_no_leak(planted_environment):
-    """credentials.test: VACUOUS - requires provider client mock."""
-    # Controller requirement: fake provider client, verify credentials not echoed
-    # This would require mocking credential store internals
-    # Marking VACUOUS until we understand the exact credentials.test path
-    pass
+    """credentials.test: assert resolved key value never appears in ActionResult.data or JSON dump."""
+    from verdict.actions import run_action
+
+    result = run_action("credentials.test", {"name": "OMNIROUTE_API_KEY"})
+    # Even if missing or tested, verify secrets aren't leaked in data representation
+    data_str = json.dumps(result.data)
+    for secret in PLANTED_SECRETS.values():
+        if secret and secret != "«redacted:sk-…»":
+            assert secret not in data_str
+    assert_no_secret_leak(result, "credentials.test")
 
 
 @pytest.mark.parametrize("harness", ["prime", "cursor", "cline", "claude", "windsurf"])
diff --git a/tests/test_credentials_test_action.py b/tests/test_credentials_test_action.py
new file mode 100644
index 00000000..1998ab39
--- /dev/null
+++ b/tests/test_credentials_test_action.py
@@ -0,0 +1,121 @@
+"""BOD-413: test credentials.test action falls through to gateway key resolver."""
+
+from __future__ import annotations
+
+from pathlib import Path
+
+import pytest
+
+from verdict import credentials_registry
+from verdict.actions import run_action
+
+FILE_KEY = "file-key-fixture"
+
+
+@pytest.fixture
+def key_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
+    config = tmp_path / "config"
+    (config / "verdict").mkdir(parents=True, mode=0o700)
+    path = config / "verdict" / "omniroute-api-key"
+    path.write_text(FILE_KEY + "\n", encoding="utf-8")
+    path.chmod(0o600)
+    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
+
+
+def test_credentials_test_resolves_key_file(
+    key_file: None, monkeypatch: pytest.MonkeyPatch
+) -> None:
+    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
+    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)
+
+    called: list[str] = []
+
+    def fake_live_check(value: str) -> tuple[bool, str]:
+        called.append(value)
+        return True, "ok"
+
+    monkeypatch.setattr(
+        credentials_registry,
+        "CREDENTIALS",
+        tuple(
+            c
+            if c.env_name != "OMNIROUTE_API_KEY"
+            else type(c)(
+                env_name=c.env_name,
+                purpose=c.purpose,
+                features=c.features,
+                optional=c.optional,
+                live_check=fake_live_check,
+            )
+            for c in credentials_registry.CREDENTIALS
+        ),
+    )
+
+    result = run_action("credentials.test", {"name": "OMNIROUTE_API_KEY"})
+    assert result.ok
+    assert result.data["status"] == "ok"
+    assert called == [FILE_KEY]
+
+
+def test_credentials_test_env_still_wins(key_file: None, monkeypatch: pytest.MonkeyPatch) -> None:
+    env_key = "env-key-sentinel"
+    monkeypatch.setenv("OMNIROUTE_API_KEY", env_key)
+    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)
+
+    called: list[str] = []
+
+    def fake_live_check(value: str) -> tuple[bool, str]:
+        called.append(value)
+        return True, "ok"
+
+    monkeypatch.setattr(
+        credentials_registry,
+        "CREDENTIALS",
+        tuple(
+            c
+            if c.env_name != "OMNIROUTE_API_KEY"
+            else type(c)(
+                env_name=c.env_name,
+                purpose=c.purpose,
+                features=c.features,
+                optional=c.optional,
+                live_check=fake_live_check,
+            )
+            for c in credentials_registry.CREDENTIALS
+        ),
+    )
+
+    result = run_action("credentials.test", {"name": "OMNIROUTE_API_KEY"})
+    assert result.ok
+    assert result.data["status"] == "ok"
+    assert called == [env_key]
+
+
+def test_credentials_test_non_gateway_name_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
+    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
+    called: list[str] = []
+
+    def fake_live_check(value: str) -> tuple[bool, str]:
+        called.append(value)
+        return True, "ok"
+
+    monkeypatch.setattr(
+        credentials_registry,
+        "CREDENTIALS",
+        tuple(
+            c
+            if c.env_name != "OPENAI_API_KEY"
+            else type(c)(
+                env_name=c.env_name,
+                purpose=c.purpose,
+                features=c.features,
+                optional=c.optional,
+                live_check=fake_live_check,
+            )
+            for c in credentials_registry.CREDENTIALS
+        ),
+    )
+    result = run_action("credentials.test", {"name": "OPENAI_API_KEY"})
+    assert not result.ok
+    assert result.data["status"] == "missing"
+    assert called == []
diff --git a/verdict/actions/extra.py b/verdict/actions/extra.py
index 6c70dcbb..4bb29b0e 100644
--- a/verdict/actions/extra.py
+++ b/verdict/actions/extra.py
@@ -159,7 +159,7 @@ def _action_credentials_test(**kwargs: Any) -> ActionResult:
     import os
 
     from verdict.credentials_registry import get_credential
-    from verdict.credentials_store import CredentialsStore
+    from verdict.credentials_store import GATEWAY_KEY_ENV_NAMES, CredentialsStore, resolve_api_key
 
     name: str = kwargs["name"]
     cred = get_credential(name)
@@ -175,6 +175,8 @@ def _action_credentials_test(**kwargs: Any) -> ActionResult:
         )
     CredentialsStore().load_into_env()
     value = os.environ.get(name)
+    if not value and name in GATEWAY_KEY_ENV_NAMES:
+        value = resolve_api_key()
     if not value:
         return ActionResult(
             data={"name": name, "status": "missing", "message": "credential not set"},

## PR #1014 — DirectGatewayExecutor resolves via credentials_store (diff below)
diff --git a/tests/test_direct_gateway_executor.py b/tests/test_direct_gateway_executor.py
index c025ba8a..772a9efc 100644
--- a/tests/test_direct_gateway_executor.py
+++ b/tests/test_direct_gateway_executor.py
@@ -1135,6 +1135,48 @@ def test_explicit_key_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
     assert DirectGatewayExecutor(api_key="explicit").api_key == "explicit"
 
 
+FILE_KEY = "file-key-fixture"
+
+
+@pytest.fixture
+def executor_key_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
+    """Owner-only omniroute-api-key under a tmp XDG_CONFIG_HOME (0700 dir, 0600 file)."""
+    config = tmp_path / "config"
+    (config / "verdict").mkdir(parents=True, mode=0o700)
+    path = config / "verdict" / "omniroute-api-key"
+    path.write_text(FILE_KEY + "\n", encoding="utf-8")
+    path.chmod(0o600)
+    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
+
+
+def test_executor_resolves_key_file_when_env_is_unset(
+    executor_key_file: None, monkeypatch: pytest.MonkeyPatch
+) -> None:
+    """The executor must use the same credential-store resolver as every other
+    transport site: with only the key file present it must send the key."""
+    for name in ("VERDICT_OMNIROUTE_API_KEY", "OMNIROUTE_API_KEY", "OPENAI_API_KEY"):
+        monkeypatch.delenv(name, raising=False)
+    executor = DirectGatewayExecutor()
+    assert executor.api_key == FILE_KEY
+    assert executor._headers()["authorization"] == f"Bearer {FILE_KEY}"
+
+
+def test_executor_env_key_beats_key_file(
+    executor_key_file: None, monkeypatch: pytest.MonkeyPatch
+) -> None:
+    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "env-key")
+    executor = DirectGatewayExecutor()
+    assert executor.api_key == "env-key"
+
+
+def test_executor_openai_key_still_last_resort(monkeypatch: pytest.MonkeyPatch) -> None:
+    for name in ("VERDICT_OMNIROUTE_API_KEY", "OMNIROUTE_API_KEY", "OPENAI_API_KEY"):
+        monkeypatch.delenv(name, raising=False)
+    monkeypatch.setenv("OPENAI_API_KEY", "a")
+    executor = DirectGatewayExecutor()
+    assert executor.api_key == "a"
+
+
 def test_cli_executor_builder_honours_verdict_key(monkeypatch: pytest.MonkeyPatch) -> None:
     """Regression: the orchestrate CLI must not override executor key precedence."""
     import argparse
diff --git a/verdict/orchestration/executors.py b/verdict/orchestration/executors.py
index 7c2be327..80f864c8 100644
--- a/verdict/orchestration/executors.py
+++ b/verdict/orchestration/executors.py
@@ -948,16 +948,10 @@ class DirectGatewayExecutor:
         self.base_url = base_url.rstrip("/")
         # The gateway this executor sends to; the runtime checks it against the permit.
         self.gateway_origin = self.base_url
-        # Same precedence as the rest of Verdict (orchestration.run.resolve_api_key):
-        # the certify workflow and operators set VERDICT_OMNIROUTE_API_KEY.
-        self.api_key = api_key or next(
-            (
-                value
-                for name in ("VERDICT_OMNIROUTE_API_KEY", "OMNIROUTE_API_KEY", "OPENAI_API_KEY")
-                if (value := os.environ.get(name, "").strip())
-            ),
-            "",
-        )
+        # The one gateway key resolver; OPENAI_API_KEY stays a last resort here.
+        from verdict.credentials_store import resolve_api_key
+
+        self.api_key = api_key or resolve_api_key() or os.environ.get("OPENAI_API_KEY", "").strip()
         self.timeout_connect = timeout_connect
 
     def _headers(self) -> dict[str, str]:
