with open('llm_gate/gate.py', 'r') as f:
    lines = f.readlines()

new_lines = []
for i, line in enumerate(lines):
    # Fix line 43 double indent
    if "        # Execute Learned Routing Prediction" in line and i == 42:
        new_lines.append("        # Execute Learned Routing Prediction\n")
        continue

    # Fix line 54 zero indent
    if line.startswith("final_tier = min(req_tier, eff_tier) if eff_tier is not None else req_tier"):
        new_lines.append("        final_tier = min(req_tier, eff_tier) if eff_tier is not None else req_tier\n")
        continue
    
    # Initialize esc_reason earlier so it exists on line 62
    if line.startswith("        req_tier = TIER_MAP.get(criticality.lower(), 2)"):
        new_lines.append(line)
        new_lines.append("        esc_reason = None\n")
        continue

    new_lines.append(line)

with open('llm_gate/gate.py', 'w') as f:
    f.writelines(new_lines)
