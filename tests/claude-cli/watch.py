"""Watch the Claude Code processes in the agent pod, for the Claude CLI test.

The test pipes this file to `python3 - <seconds>` in the agent container.
It prints one JSON line for each new `claude` process: its arguments, the
names of its environment variables, and the value of a few variables that
hold no secret. It never prints the value of any other variable.
"""

import json
import os
import sys
import time

SHOW = ("ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR", "HOME")


def is_claude(argv):
    return bool(argv) and os.path.basename(argv[0]) == "claude"


def read(path):
    with open(path, "rb") as f:
        return f.read()


def cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def main():
    end = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 3600)
    seen = set()
    while time.time() < end:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                argv = [
                    a.decode() for a in read(f"/proc/{pid}/cmdline").split(b"\0") if a
                ]
            except OSError:
                continue
            if not is_claude(argv):
                continue
            if pid in seen:
                continue
            try:
                env = dict(
                    e.decode().split("=", 1)
                    for e in read(f"/proc/{pid}/environ").split(b"\0")
                    if b"=" in e
                )
            except OSError:
                continue
            seen.add(pid)
            raw = read(f"/proc/{pid}/cmdline").split(b"\0")[:-1]
            print(
                json.dumps(
                    {
                        "kind": "process",
                        "time": time.time(),
                        "pid": pid,
                        "argv": [a.decode() for a in raw],
                        "cwd": cwd(pid),
                        "env_names": sorted(env),
                        "env": {k: env[k] for k in SHOW if k in env},
                    }
                ),
                flush=True,
            )
        time.sleep(0.05)


if __name__ == "__main__":
    main()
