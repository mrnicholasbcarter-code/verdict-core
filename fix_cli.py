with open('llm_gate/cli.py', 'r') as f:
    orig = f.read()
    
broken = """        if terse:
        dec = gate.route(task, criticality)
        print(dec.model)
        return
with console.status("[bold green]Evaluating network & heuristics...", spinner="dots"):"""

fixed = """        if terse:
            dec = gate.route(task, criticality)
            print(dec.model)
            return
            
        with console.status("[bold green]Evaluating network & heuristics...", spinner="dots"):"""

fixed_content = orig.replace(broken, fixed)
with open('llm_gate/cli.py', 'w') as f:
    f.write(fixed_content)
