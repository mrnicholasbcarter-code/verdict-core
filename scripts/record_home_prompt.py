#!/usr/bin/env python3
"""Record the home command prompt demo as an asciinema v2 cast."""
from __future__ import annotations
import json, os, pty, select, signal, struct, sys, termios, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 110, 40

def set_pty_size(fd, rows, cols):
    import fcntl
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

def read_pty_with_input(argv, env, inputs, *, rows=HEIGHT, cols=WIDTH, timeout=60.0):
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        set_pty_size(sys.stdout.fileno(), rows, cols)
        os.environ.clear()
        os.environ.update(env)
        os.execvp(argv[0], argv)
    set_pty_size(master_fd, rows, cols)
    chunks = []
    start = time.monotonic()
    input_idx = 0
    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout: break
            while input_idx < len(inputs):
                if elapsed >= inputs[input_idx][0]:
                    os.write(master_fd, inputs[input_idx][1].encode())
                    input_idx += 1
                else: break
            wait = max(0.01, min((inputs[input_idx][0] - elapsed) if input_idx < len(inputs) else 1.0, 0.5))
            ready, _, _ = select.select([master_fd], [], [], wait)
            if ready:
                try:
                    data = os.read(master_fd, 65536)
                    if not data: break
                    chunks.append((time.monotonic() - start, data))
                except OSError: break
            try:
                wpid, _ = os.waitpid(child_pid, os.WNOHANG)
                if wpid != 0:
                    while True:
                        r, _, _ = select.select([master_fd], [], [], 0.1)
                        if not r: break
                        try:
                            d = os.read(master_fd, 65536)
                            if not d: break
                            chunks.append((time.monotonic() - start, d))
                        except OSError: break
                    break
            except ChildProcessError: break
    finally:
        try: os.kill(child_pid, signal.SIGTERM)
        except ProcessLookupError: pass
        os.close(master_fd)
    return chunks

def to_asciicast(chunks, *, width, height, title):
    header = json.dumps({"version": 2, "width": width, "height": height, "title": title,
        "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"}})
    lines = [header]
    for ts, data in chunks:
        lines.append(json.dumps([round(ts, 6), "o", data.decode("utf-8", errors="replace")]))
    return "\n".join(lines) + "\n"

def main():
    output = ROOT / "docs" / "assets" / "home-prompt.cast"
    child_script = str(ROOT / "scripts" / "_record_home_child.py")
    python = "/home/nick/dev/verdict-core/.venv/bin/python"

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(ROOT),
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "COLUMNS": str(WIDTH),
        "LINES": str(HEIGHT),
        "PYTHONUNBUFFERED": "1",
        "PROMPT_TOOLKIT_NO_CPR": "1",
        "VERDICT_GATEWAY": "http://127.0.0.1:20128",
    }

    # Scripted inputs: (delay_seconds, input_text)
    inputs = [
        (3.5, "/help\n"),            # show commands
        (6.0, "/demo\n"),            # run demo (takes ~2s)
        (11.0, "/config\n"),         # show config
        (14.0, "build a website\n"), # free text goal
        (16.0, "n\n"),              # decline
        (18.0, "\x04"),             # Ctrl-D to exit
    ]

    argv = [python, child_script]
    print(f"Recording home prompt demo into {WIDTH}x{HEIGHT} pty", file=sys.stderr)
    chunks = read_pty_with_input(argv, env, inputs, timeout=30.0)
    if not chunks:
        print("No output captured", file=sys.stderr)
        return 1

    body = to_asciicast(chunks, width=WIDTH, height=HEIGHT,
        title="Verdict: interactive command prompt")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    duration = chunks[-1][0] if chunks else 0
    print(f"wrote {output} ({len(chunks)} frames, {duration:.1f}s)", file=sys.stderr)

    # Show timing stats
    ts_list = [c[0] for c in chunks]
    first_3s = [t for t in ts_list if t < 3.0]
    if first_3s:
        print(f"First 3s: {len(first_3s)} frames, span {first_3s[-1]-first_3s[0]:.2f}s", file=sys.stderr)
    return 0

if __name__ == "__main__":
    sys.exit(main())
