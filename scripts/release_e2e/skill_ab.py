#!/usr/bin/env python3
"""skill_ab — ask the release check's questions of two versions of the skills, N times each.

A real `/ledger` or `/receipts` session is nondeterministic and the judge is a
model, so one run per question says little about whether a change to a skill
helped. This asks every question N times of each version, in real sessions with
that version's plugin installed, has the stage-3 judge mark every answer, and
prints the comparison: key points hit, claims resting on no record, whether the
runs of one question agreed, and two clarity readings (the first sentence
answers; how it came to be is told in date order).

Usage:
  python3 scripts/release_e2e/skill_ab.py --variant old=<git-ref> --variant new=<git-ref> \\
      [--runs N] [--model M] [--keep]

Each variant is a clone of this repository at its ref, installed as a plugin
into a HOME and a Claude configuration of its own. The dummy project is built
once, with the last variant, and both are asked about the same ledger.
Everything runs in one temporary directory; the developer's workspace is checked
untouched at the end with scripts/home_guard.py. The rows, the answers and the
judge's cards are kept in ~/.cache/provledger/release-checks/ab-<stamp>/.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def aggregate(rows: list[dict], *, runs: int, questions: list[str], variants: list[str] | None = None) -> dict:
    """{variant: {question: {...}, "_total": {...}, "_missing": [(question, run)]}}.

    A run that produced no answer is reported in `_missing`, never averaged in:
    a timeout is not a zero, and folding it into the score would hide it."""
    variants = variants or sorted({r["variant"] for r in rows})
    out: dict = {}
    for v in variants:
        mine = [r for r in rows if r["variant"] == v and r.get("outcome") == "ok"]
        per: dict = {}
        for q in questions:
            got = [r for r in mine if r["id"] == q]
            per[q] = {
                "runs": len(got),
                "hits": sum(r["hits"] for r in got),
                "points": sum(r["points"] for r in got),
                "unsupported": sum(1 for r in got if r["unsupported"]),
                "findings": sum(1 for r in got if r["verdict"] == "FINDING"),
                "agree": len({r["verdict"] for r in got}) <= 1,
                "answer_first": sum(1 for r in got if (r.get("clarity") or {}).get("answer_first")),
                "date_order": sum(1 for r in got if (r.get("clarity") or {}).get("date_order")),
            }
        total = {k: sum(per[q][k] for q in questions)
                 for k in ("runs", "hits", "points", "unsupported", "findings", "answer_first", "date_order")}
        done = {(r["id"], r["run"]) for r in mine}
        per["_total"] = total
        per["_missing"] = [(q, n) for q in questions for n in range(1, runs + 1) if (q, n) not in done]
        out[v] = per
    return out


def _table(agg: dict, questions: list[str]) -> str:
    vs = list(agg)
    head = f"{'question':<30}" + "".join(f"{v + ' hit':>10}{v + ' bad':>9}{v + ' 1st':>9}{v + ' date':>10}{v + ' =':>7}"
                                         for v in vs)
    lines = [head, "-" * len(head)]
    for q in questions + ["_total"]:
        cells = ""
        for v in vs:
            c = agg[v][q]
            cells += (f"{c['hits']:>6}/{c['points']:<3}{c['unsupported']:>9}{c['answer_first']:>9}"
                      f"{c['date_order']:>10}{('yes' if c.get('agree', True) else 'no') if q != '_total' else '':>7}")
        lines.append(f"{q:<30}{cells}")
    for v in vs:
        if agg[v]["_missing"]:
            lines.append(f"  {v}: no answer for {agg[v]['_missing']}")
    lines.append("hit = key points hit / asked · bad = answers with a claim resting on no record · "
                 "1st = first sentence answers · date = told in date order · = = runs agreed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--variant", action="append", required=True, help="name=git-ref, twice")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--model", default=None)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    variants = [tuple(v.split("=", 1)) for v in a.variant]

    real_home = Path.home()
    stamp = time.strftime("%y%m%d%H%M%S")
    root = Path(tempfile.mkdtemp(prefix="provledger-skill-ab."))
    keep = real_home / ".cache" / "provledger" / "release-checks" / f"ab-{stamp}"
    guard = root / "home-guard.json"
    subprocess.run([sys.executable, str(REPO / "scripts" / "home_guard.py"), "snapshot", str(guard)],
                   env={**os.environ, "PROVLEDGER_GUARD_HOME": str(real_home / "skill-workspace")}, check=True)

    # The same isolation release-e2e.sh sets up, before the stage modules read it at import.
    home = root / "home"
    reg = home / "skill-workspace" / "project-graphs"
    reg.mkdir(parents=True)
    os.environ.update({
        "E2E_ROOT": str(root), "E2E_LOGS": str(root / "logs"), "E2E_FINDINGS": str(root / "findings.txt"),
        "E2E_REAL_HOME": str(real_home), "E2E_NONCE": f"ab{stamp}", "E2E_KEEP_DIR": str(keep),
        "ORCH_DB": str(home / "skill-workspace" / "orchestrator.db"),
        "PSG_REGISTRY_ROOT": str(reg), "PSG_REGISTRY_PATH": str(reg / "projects.json"),
        "PSG_INDEX_PATH": str(reg / "PROJECT-STATE-GRAPHS.md"),
        "PROVLEDGER_HOOK_ERRORS": str(root / "hook-errors.log"),
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR") or str(real_home / ".cache" / "uv"),
        "HOME": str(home), "CLAUDE_CONFIG_DIR": str(root / "judge-config"),
        "PATH": f"{real_home / '.local' / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
    })
    os.environ.pop("VIRTUAL_ENV", None)
    (root / "logs").mkdir()
    sys.path.insert(0, str(HERE))
    import dummy_project as DP
    import plugin_session as PS
    import stage2_surfaces as S2
    import stage3_judge as S3
    from e2elib import OK

    PS.seed_config(root / "judge-config", real_home)      # the judge's calls: logged in, no plugins
    os.environ["PROVLEDGER_CLAUDE_SETTINGS"] = str(root / "judge-config" / "settings.json")
    say = lambda m: print(m, flush=True)                  # noqa: E731

    installed = {}
    for name, ref in variants:
        src, vhome, conf = root / f"src-{name}", root / f"home-{name}", root / f"config-{name}"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(REPO), str(src)], check=True)
        subprocess.run(["git", "-C", str(src), "checkout", "-q", ref], check=True)
        vhome.mkdir()
        PS.seed_config(conf, real_home)
        env = PS.stranger_env(vhome, conf)
        env["PROVLEDGER_BOOTSTRAP_LOG"] = str(root / "logs" / f"bootstrap-{name}.log")
        rc, said = PS.install_plugin(src, env)
        if rc != 0:
            say(f"{name}: the plugin did not install\n{said[-800:]}")
            return 3
        boot = subprocess.run(["bash", str(src / "scripts" / "bootstrap.sh")], env=env, cwd=str(vhome),
                              capture_output=True, text=True)
        py = vhome / "skill-workspace" / ".venv" / "bin" / "python"
        if boot.returncode != 0 or not py.exists():
            say(f"{name}: the plugin venv was not built\n{(boot.stdout + boot.stderr)[-800:]}")
            return 3
        installed[name] = {"src": src, "home": vhome, "config": conf, "python": str(py), "ref": ref,
                           "sha": subprocess.run(["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
                                                 capture_output=True, text=True).stdout.strip()}
        say(f"{name} = {ref} ({installed[name]['sha']}) installed")

    builder = installed[variants[-1][0]]
    say(f"building the dummy project with {variants[-1][0]}")
    facts = DP.build(root / "dummy", builder["src"], builder["python"], os.environ["E2E_NONCE"], say=say)
    qs = DP.questions(facts)
    ids = [q["id"] for q in qs]

    rows, answers = [], []
    for run in range(1, a.runs + 1):
        for name, _ in variants:
            v = installed[name]
            env = PS.stranger_env(v["home"], v["config"])
            for k in ("ORCH_DB", "PSG_REGISTRY_ROOT", "PSG_REGISTRY_PATH", "PSG_INDEX_PATH"):
                env[k] = os.environ[k]
            for q in qs:
                slash = S2.SLASH[q["surface"]]
                s = PS.run_session(f"/{slash} {q['question']}", cwd=Path(facts["repo"]), env=env,
                                   allowed_tools=S2.S4_TOOLS, max_turns=40, timeout_s=420, model=a.model,
                                   log=root / "logs" / f"ab-{name}-{run}-{q['id']}.jsonl")
                ans = {**S2.real_answer(q, s), "variant": name, "run": run}
                answers.append(ans)
                row = {"variant": name, "run": run, "id": q["id"], "outcome": ans["outcome"]}
                if ans["outcome"] == "ok":
                    card, detail, outcome = S3.judge_one(ans)
                    if outcome == "ok":
                        pts = card.get("points") or []
                        row.update({"hits": sum(1 for p in pts if str(p.get("verdict")).lower() == "hit"),
                                    "points": len(pts),
                                    "unsupported": bool((card.get("unsupported_claim") or {}).get("found")),
                                    "verdict": "OK" if S3.judge_verdict(card) == OK else "FINDING",
                                    "clarity": card.get("clarity") or {}, "card": card})
                    else:
                        row["outcome"] = f"judge {outcome}"
                rows.append(row)
                say(f"run {run} · {name} · {q['id']} · {row['outcome']}"
                    + (f" · {row['hits']}/{row['points']}" + (" · unsupported" if row["unsupported"] else "")
                       if row["outcome"] == "ok" else ""))

    agg = aggregate(rows, runs=a.runs, questions=ids, variants=[n for n, _ in variants])
    print()
    print(_table(agg, ids))
    keep.mkdir(parents=True, exist_ok=True)
    (keep / "ab.json").write_text(json.dumps({
        "variants": {n: {"ref": installed[n]["ref"], "sha": installed[n]["sha"]} for n, _ in variants},
        "runs": a.runs, "aggregate": agg, "rows": rows, "answers": answers}, indent=1, default=str),
        encoding="utf-8")
    for f in (root / "logs").glob("ab-*.jsonl"):
        (keep / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"\nkept for review: {keep}")

    os.environ["HOME"] = str(real_home)
    g = subprocess.run([sys.executable, str(REPO / "scripts" / "home_guard.py"), "check", str(guard)],
                       env={**os.environ, "PROVLEDGER_GUARD_HOME": str(real_home / "skill-workspace")},
                       capture_output=True, text=True)
    print("home guard: " + ("the developer's workspace shows nothing of this run" if g.returncode == 0
                            else g.stdout + g.stderr))
    if a.keep:
        print(f"sandbox kept: {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if g.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
