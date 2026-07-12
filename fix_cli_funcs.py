with open('llm_gate/cli.py', 'r') as f:
    orig = f.read()

fixed_content = orig.replace('run_setup()', 'cmd_setup()')
fixed_content = fixed_content.replace('handle_route(args.task, args.criticality, getattr(args, "terse", False))', 'cmd_route(args.task, args.criticality, getattr(args, "terse", False))')
fixed_content = fixed_content.replace('handle_stats(args.log_path)', 'cmd_stats(args.log_path)')

with open('llm_gate/cli.py', 'w') as f:
    f.write(fixed_content)
