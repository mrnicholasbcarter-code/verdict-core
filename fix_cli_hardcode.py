with open('llm_gate/cli.py', 'r') as f:
    orig = f.read()

broken = """        gate = Gate(
            primary_model="anthropic/claude-3-opus",
            providers={
                "public_ollama": ProviderConfig(base_url="http://localhost:11434/v1")
            }
        )"""

fixed = """        gate = Gate() # Automatically loads config via Gate(None)"""

fixed_content = orig.replace(broken, fixed)
with open('llm_gate/cli.py', 'w') as f:
    f.write(fixed_content)
