# Credentials and Dependencies

Verdict's credential registry captures every API key, token, and secret the system reads, along with optional dependencies like CLI tools and Python extras.

## Quick Start

```bash
# List all registered credentials and their status
verdict credentials list

# Set a credential (hidden prompt)
verdict credentials set OMNIROUTE_API_KEY

# Set from stdin (for scripts)
echo "your-key" | verdict credentials set OMNIROUTE_API_KEY --stdin

# Test a credential with its live check
verdict credentials test OMNIROUTE_API_KEY

# Remove a credential
verdict credentials unset OMNIROUTE_API_KEY

# Interactive setup for missing credentials
verdict setup credentials

# Check status in doctor
verdict doctor
```

## Credential Storage

Credentials are stored in `$XDG_CONFIG_HOME/verdict/credentials.env` (typically `~/.config/verdict/credentials.env`).

### Security

- **Directory permissions**: `0700` (owner only)
- **File permissions**: `0600` (owner read/write only)
- **Atomic writes**: Via temp file + `os.replace()` to prevent corruption
- **Permission enforcement**: Refuses to load when group/world readable
- **Never in YAML**: Secrets are never written to `verdict.yaml`
- **Never in argv**: Set via `getpass()` or `--stdin`, never command arguments

If permissions are too permissive, you'll see:

```
Credential store has insecure permissions: 0o644
Fix with: chmod 0600 /home/user/.config/verdict/credentials.env
```

## Precedence

Exported environment variables **always win** over stored values. The store only fills unset names. It's loaded once at CLI and server startup.

## Registered Credentials

All credentials used by Verdict must be registered in `verdict/credentials_registry.py`. The drift test ensures no credential is read without being registered.

## Dependencies

The registry also tracks optional dependencies. Check their status with `verdict setup credentials` or `verdict doctor`.

## Masking

All credential values are masked in output: `first8chars...[len=N]`. Full secrets never appear in logs, doctor output, or `verdict.yaml`.

## API

See inline documentation in `verdict/credentials_store.py` and `verdict/credentials_registry.py`.
