"""plugin_session — a real headless session with the plugin installed, and what it did.

Stage 0 installs the plugin the way a user does and stage 2 asks `/ledger` and
`/receipts` as real slash commands. Both judge a session by what it actually
did, never by what it says it did: which plugins were loaded, which commands it
ran and in what order, what each one printed, whether it failed, and what the
session finally answered. `claude -p --output-format stream-json --verbose`
prints all of that, one JSON event per line; `parse_stream` reads it.

The sessions run in a configuration of their own — HOME and CLAUDE_CONFIG_DIR
inside the sandbox, seeded with the login and nothing else — so the host's
plugins cannot answer in place of the one under test. (`--settings` with
`"enabledPlugins": {}` does not do that: settings merge, and an empty map
disables nothing.)
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

ACCOUNT_KEYS = ("oauthAccount", "userID", "installMethod", "firstStartTime")
PLUGIN = "provledger@provledger"

# What a stranger's shell does not have: the developer's ledger, registry and
# venv paths, the check's own switches, and the variables a plugin hook or a
# step runner set in the shell that launched the check.
NOT_A_STRANGERS = ("ORCH_DB", "PROVLEDGER_VENV", "PSG_REGISTRY_ROOT", "PSG_REGISTRY_PATH",
                   "PSG_INDEX_PATH", "PROVLEDGER_HOOK_ERRORS", "PROVLEDGER_HEADLESS",
                   "PROVLEDGER_CLAUDE_SETTINGS", "VIRTUAL_ENV", "CLAUDE_PLUGIN_ROOT",
                   "PROVLEDGER_ASK_RUNNER", "PYTHONPATH")


def seed_config(config_dir: Path, real_home: Path) -> None:
    """A Claude configuration that is logged in and holds nothing else.

    The credential file and the account stanza only — no projects, no theme, no
    plugins — and a settings file that fixes the language (a host preference
    once made every answer come back in Chinese) without an `enabledPlugins`
    key, because the plugin under test is installed into this config next."""
    config_dir, real_home = Path(config_dir), Path(real_home)
    config_dir.mkdir(parents=True, exist_ok=True)
    cred = real_home / ".claude" / ".credentials.json"
    if cred.is_file():
        shutil.copy2(cred, config_dir / ".credentials.json")
    account: dict = {}
    for src in (real_home / ".claude" / ".claude.json", real_home / ".claude.json"):
        try:
            d = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        account = {k: d[k] for k in ACCOUNT_KEYS if k in d}
        break
    account["hasCompletedOnboarding"] = True
    (config_dir / ".claude.json").write_text(json.dumps(account), encoding="utf-8")
    (config_dir / "settings.json").write_text(json.dumps({"language": "en"}), encoding="utf-8")


def stranger_env(home: Path, config_dir: Path, base: dict | None = None) -> dict:
    """The environment of someone who has only installed the plugin.

    PATH keeps everything except a directory that already holds a `provledger`:
    the point of stage 0 is that the plugin itself puts one there."""
    env = dict(os.environ if base is None else base)
    for k in NOT_A_STRANGERS:
        env.pop(k, None)
    env["HOME"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["PATH"] = os.pathsep.join(
        d for d in env.get("PATH", "").split(os.pathsep) if d and not (Path(d) / "provledger").exists())
    return env


def claude_bin() -> str:
    return shutil.which("claude") or "claude"


def install_plugin(source: str | Path, env: dict, *, timeout_s: float = 300) -> tuple[int, str]:
    """`claude plugin marketplace add <source>` then `claude plugin install`, as
    INSTALL.md tells a user to. Returns the first failing exit code (or 0) and
    the transcript of both commands."""
    said = []
    for argv in (["plugin", "marketplace", "add", str(source)], ["plugin", "install", PLUGIN]):
        p = subprocess.run([claude_bin(), *argv], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout_s)
        said.append(f"$ claude {' '.join(argv)}\nrc={p.returncode}\n{p.stdout}{p.stderr}")
        if p.returncode != 0:
            return p.returncode, "\n".join(said)
    return 0, "\n".join(said)


def plugin_list(env: dict) -> str:
    p = subprocess.run([claude_bin(), "plugin", "list"], env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60)
    return p.stdout + p.stderr


@dataclass
class Call:
    id: str
    name: str
    input: dict
    output: str = ""
    is_error: bool = False


@dataclass
class Transcript:
    session_id: str = ""
    plugins: dict = field(default_factory=dict)          # name -> path
    slash_commands: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    result: str = ""
    num_turns: int | None = None
    ok: bool = False
    permission_denials: list = field(default_factory=list)
    rc: int | None = None
    elapsed_s: float = 0.0
    stderr: str = ""
    raw_path: str = ""

    def bash_commands(self) -> list[str]:
        return [c.input.get("command", "") for c in self.calls if c.name == "Bash"]

    def provledger_calls(self) -> list[Call]:
        """The calls that read the ledger: a successful Bash call with at least one
        `provledger <subcommand>` in it, wherever it sits in the command."""
        return [c for c in self.calls if c.name == "Bash" and not c.is_error and _reads(c)]

    def invocations(self) -> list[str]:
        """Every `provledger` read the session ran, in order, as the text after
        `provledger` — `receipts candidates "…" --project p`. Agents write
        `P=x; provledger …` and `a; echo ---; provledger …`, so a check that only
        looks at how a command starts misses reads that did run. A flag
        (`--help`) is not a read, and a failed call read nothing."""
        return [i for c in self.provledger_calls() for i in _reads(c)]

    def provledger_material(self) -> str:
        """What the session read from the ledger, each output under the command
        that printed it — the evidence stage 3 marks an answer's reasons against."""
        return "\n\n".join(f"$ {c.input.get('command', '')}\n{c.output}" for c in self.provledger_calls())

    def code_material(self) -> str:
        """What the session read that was not the ledger: files it opened, greps it
        ran. /receipts may use the code for what and where, never for why (its
        hard rule 2), so stage 3 is shown this apart from the ledger reads."""
        out = []
        for c in self.calls:
            if c.is_error or not c.output or c in self.provledger_calls():
                continue
            if c.name == "Bash":
                out.append(f"$ {c.input.get('command', '')}\n{c.output}")
            elif c.name in ("Read", "Grep", "Glob"):
                what = c.input.get("file_path") or c.input.get("pattern") or ""
                out.append(f"[{c.name} {what}]\n{c.output}")
        return "\n\n".join(out)


def _segments(command: str) -> list[str]:
    """The simple commands in a shell line, split on ; & | and newlines outside
    quotes. Close enough for reading what an agent ran; not a shell parser."""
    out, cur, quote, i = [], [], None, 0
    while i < len(command):
        ch = command[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                cur.append(command[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch in ";&|\n":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _reads(call: Call) -> list[str]:
    """The `provledger` reads in one Bash call: the text after `provledger` in
    each simple command that runs it, flags (`--help`) left out."""
    found = []
    for seg in _segments(call.input.get("command", "")):
        if not _is_provledger(seg):
            continue
        words = seg.split()
        while words and os.path.basename(words[0]) != "provledger":
            words = words[1:]
        rest = seg[seg.find(words[0]) + len(words[0]):].strip() if words else ""
        if rest and not rest.startswith("-"):
            found.append(rest)
    return found


def _is_provledger(command: str) -> bool:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    while words and "=" in words[0] and not words[0].startswith("="):
        words = words[1:]                                  # FOO=bar provledger …
    return bool(words) and os.path.basename(words[0]) == "provledger"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_stream(lines) -> Transcript:
    t = Transcript()
    by_id: dict[str, Call] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            t.session_id = ev.get("session_id") or t.session_id
            t.plugins = {p["name"]: p.get("path") for p in ev.get("plugins") or []
                         if isinstance(p, dict) and p.get("name")}
            t.slash_commands = list(ev.get("slash_commands") or [])
        elif kind in ("assistant", "user"):
            content = (ev.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    c = Call(id=b.get("id") or "", name=b.get("name") or "", input=b.get("input") or {})
                    t.calls.append(c)
                    by_id[c.id] = c
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                    c = by_id[b["tool_use_id"]]
                    c.output, c.is_error = _text(b.get("content")), bool(b.get("is_error"))
        elif kind == "result":
            t.result = ev["result"] if isinstance(ev.get("result"), str) else ""
            t.num_turns = ev.get("num_turns")
            t.ok = ev.get("subtype") == "success" and not ev.get("is_error")
            t.permission_denials = list(ev.get("permission_denials") or [])
            t.session_id = ev.get("session_id") or t.session_id
    return t


def run_session(prompt: str, *, cwd: Path, env: dict, allowed_tools: tuple[str, ...] = (),
                max_turns: int = 30, timeout_s: float = 300, model: str | None = None,
                log: Path | None = None) -> Transcript:
    """One headless session. The raw stream is kept at `log` so a person can
    read exactly what the session did when a check below disagrees with it."""
    cmd = [claude_bin(), "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--max-turns", str(max_turns)]
    if model:
        cmd += ["--model", model]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]
    started = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout_s)
        out, err, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err, rc = f"no result within {timeout_s:g}s", None
    t = parse_stream(out.splitlines())
    t.rc, t.elapsed_s, t.stderr = rc, round(time.monotonic() - started, 1), (err or "")[-2000:]
    if log is not None:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text(out, encoding="utf-8")
        t.raw_path = str(log)
    return t


# ── the dashboards these sessions start ───────────────────────────────────────
def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_get(url: str, timeout: float = 5) -> tuple[int, str]:
    """(status, body); (0, reason) when nothing answered."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except OSError as e:
        return 0, f"{type(e).__name__}: {e}"


def stop_port(port: int) -> list[str]:
    """Stop whatever listens on `port` — a dashboard a session launched in the
    background outlives the session, and must not outlive the check."""
    p = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
    pids = [x for x in p.stdout.split() if x.isdigit()]
    for pid in pids:
        subprocess.run(["kill", pid], capture_output=True)
    return pids
