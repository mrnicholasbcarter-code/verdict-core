"""compiled pack injection into upstream payloads is verifiable and honest."""

from __future__ import annotations

import hashlib

from verdict.context_inject import envelope_digest, extract_envelope, inject_context_pack

PROMPT = "[INSTRUCTIONS:task (source: urn:verdict:task)]\nsummarize\n\n[EVIDENCE:adr]\nADR body\n"
DIGEST = f"sha256:{hashlib.sha256(PROMPT.encode('utf-8')).hexdigest()}"


def _inject(payload, *, surface="chat", state="hydrated", prompt=PROMPT, digest=DIGEST, ok=True):
    return inject_context_pack(
        payload,
        surface=surface,
        compiled_prompt=prompt,
        pack_state=state,
        pack_digest="sha256:" + "a" * 64,
        prompt_digest=digest,
        task_complete=ok,
    )


def test_chat_injection_prepends_system_envelope_without_mutating_input() -> None:
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    forwarded, record = _inject(payload)
    assert record.injected is True
    assert record.digest_match is True
    assert record.envelope_digest == envelope_digest(PROMPT) == DIGEST
    assert forwarded["messages"][0] == {"role": "system", "content": PROMPT}
    assert forwarded["messages"][1:] == payload["messages"]
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert extract_envelope(forwarded, surface="chat") == PROMPT


def test_responses_injection_handles_string_and_list_input() -> None:
    as_string = {"model": "m", "input": "hi"}
    forwarded, record = _inject(as_string, surface="responses")
    assert record.injected
    assert forwarded["input"] == [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": "hi"},
    ]
    as_list = {"model": "m", "input": [{"role": "user", "content": "hi"}]}
    forwarded, _ = _inject(as_list, surface="responses")
    assert forwarded["input"][0]["role"] == "system"
    assert forwarded["input"][1:] == as_list["input"]
    assert extract_envelope(forwarded, surface="responses") == PROMPT


def test_partial_is_injected_but_labelled_partial() -> None:
    _, record = _inject({"messages": []}, state="partial")
    assert record.injected is True
    assert record.pack_state == "partial"
    assert record.to_dict()["pack_state"] == "partial"


def test_empty_failed_or_task_incomplete_packs_are_never_claimed_injected() -> None:
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    for state in ("empty", "failed", None):
        forwarded, record = _inject(payload, state=state)
        assert record.injected is False
        assert forwarded is payload
        assert record.reason == f"pack_state_{state or 'unknown'}"
    forwarded, record = _inject(payload, ok=False)
    assert record.injected is False
    assert record.reason == "task_instructions_omitted"
    assert forwarded is payload


def test_prompt_digest_mismatch_refuses_injection() -> None:
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    forwarded, record = _inject(payload, digest="sha256:" + "0" * 64)
    assert record.injected is False
    assert record.reason == "prompt_digest_mismatch"
    assert record.digest_match is False
    assert forwarded is payload
