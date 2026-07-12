with open('llm_gate/cli.py', 'r') as f:
    orig = f.read()

broken = """        with console.status("[bold green]Evaluating network & heuristics...", spinner="dots"):
        dec = gate.route(task, criticality)"""

fixed = """        with console.status("[bold green]Evaluating network & heuristics...", spinner="dots"):
            dec = gate.route(task, criticality)"""

fixed_content = orig.replace(broken, fixed)
with open('llm_gate/cli.py', 'w') as f:
    f.write(fixed_content)
