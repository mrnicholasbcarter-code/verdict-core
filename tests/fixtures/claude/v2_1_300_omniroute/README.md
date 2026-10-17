# Claude Code 2.1.300 live OmniRoute hook payloads (2026-10-16 19:23 ET)

Real hook stdin JSON from Claude Code 2.1.300 in an isolated two-session smoke
(own HOME and CLAUDE_CONFIG_DIR per run; operator config and OmniRoute untouched).

- proj-a session `9c4f5406-5070-472b-81a7-37711d9d6840`
- proj-b session `2f5b4844-b5b0-46c3-99c3-35341915cad5`

Each session fired SessionStart, UserPromptSubmit, PreToolUse/PostToolUse (Skill, then Read),
Stop and SessionEnd. SubagentStop and PreCompact did not fire.

**Model caveat:** the backend model was `agy/gemini-3.1-flash-lite` through the OmniRoute
gateway, because the `agy/` Claude routes returned 429 (quota). This proves Claude Code's
hook surface and payload shapes, not the behaviour of a Claude model.

**Anonymization:** the operator home directory was replaced by `/home/USER`, and the encoded project
directory segment for it by `-home-USER-` (in `transcript_path`). Otherwise verbatim.
No API key or token appears in any file.

Original (pre-anonymization) sha256:

```
c50e0df98a8cd061ed5987b479f3509293067d5c925e491c2ac96f4f6268fbff  1792193010597757357-SessionStart.json
1f85a05bc013d6a2f9ec7a1c9ba88a09f551f93e1279ef9bc93834d62120a46c  1792193010695912154-UserPromptSubmit.json
b35d24f9d24b21056f97434399079f097d747b18dc53f7d259884d43de5ca7ff  1792193015127549820-PreToolUse.json
1a60acba5df7dfc46f3782dda899fdc2cdc491925c833e89300aff477a210e49  1792193015283884830-PostToolUse.json
c44800bc5f67a368590126b35550458824b69cf9d41685ac713cd1ab6e8f7900  1792193017710855346-PreToolUse.json
11dfbd78b4bbd8ad17851986182da2fa8f6e88ba256ada0efd7ed8580ddb3509  1792193017776709905-PostToolUse.json
820caec0c8d6de4a98d96fff945e9e2e1dc97848ebf29a5bb821063290e9f238  1792193026881716555-Stop.json
641265737bb93d65c957085f5cf62ad8810fdc3cce7bb648661b48bc72e80efb  1792193026928500359-SessionEnd.json
42110ebe4f320d4f5778609a61a812eb08b3c024ccd3cfa0a32b591d5982cdbc  1792193031524211834-SessionStart.json
ca35772a91df3f799cb18c7b195d5bc42c3a71df977f346a01d49b1673b58d27  1792193031693784395-UserPromptSubmit.json
9164f1503594ba089f3429d20ce611bd5f73e56d22496a1b063e0f62d860c394  1792193035891203016-PreToolUse.json
53e019faa7bc97bd7408180642f78446c7908d5acfe2f739436099a4d175311a  1792193036018293954-PostToolUse.json
cf73fd3acb2c9f092ac035c5e9063fb709b77c3d164f7c4b8590cac7b037573f  1792193036062916707-PreToolUse.json
0c95da720c8d9b754a5cc53df2d1b7829a1cc75ee0c0b872266b152b6223bb01  1792193036121175691-PostToolUse.json
d2356720c3c21ac21ec126b8284ccddf2b2bf28ac6fbf091485a3fa47093b61b  1792193042597164370-Stop.json
adfd141d75035769ca67c5a056d220b3dd53f1abb83c3d6a166271bd8bbc2948  1792193042649384938-SessionEnd.json
```

Anonymized sha256 values are recorded in the lane HANDOFF.
