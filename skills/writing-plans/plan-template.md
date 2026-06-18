# Plan: <topic>

> **Created:** YYYY-MM-DD HH:MM
> **Workspace:** `~/skill-workspace/plans/YYYY-MM-DD-<topic>.md`
> **Status:** 🚧 in-progress
> **Owner agent:** <your-agent-id>
> **Triggered by:** <one-line user request that started this>

## Goal

<One sentence describing what this builds.>

## Architecture

<2–3 sentences about the approach + key tech choices. What components, how they
talk, where data lives.>

## Tech Stack

- **Language/runtime:** <e.g. Python 3.11, uv-managed venv>
- **Frameworks:** <e.g. Flask 3, OpenCV 4>
- **Storage:** <e.g. local FS at ~/skill-workspace/...>
- **Deploy target:** <e.g. cloud VM, local only>

---

## Status Dashboard

| # | Step | Status | Started | Completed |
|---|------|--------|---------|-----------|
| 1 | <step name> | ⏳ not-started | — | — |
| 2 | <step name> | ⏳ not-started | — | — |
| 3 | <step name> | ⏳ not-started | — | — |
| 4 | <step name> | ⏳ not-started | — | — |

**Legend:** ⏳ not-started · 🚧 in-progress · ✅ completed · ❌ blocked · ⏭️ skipped

---

## Steps (full detail)

### 1. <step name>  ⏳ not-started

**What:** <description of the action>

**Files touched:**
- Create: `path/to/new_file.py`
- Modify: `path/to/existing.py:42-58`
- Test:   `tests/test_thing.py`

**Verification:**
```bash
pytest tests/test_thing.py -v
# Expected: 3 passed
```

**Recent log:**
```
(executing-plans appends last ~20 lines of relevant tool/shell output here
 while this step is 🚧 in-progress, then leaves it as evidence after ✅)
```

---

### 2. <step name>  ⏳ not-started

**What:** <description>

**Files touched:**
- Modify: `path/to/file.py`

**Verification:**
```bash
<command>
# Expected: <expected output>
```

**Recent log:**
```
```

---

### 3. <step name>  ⏳ not-started

**What:** <description>

**Files touched:**
- Create: `path/to/new_file.py`

**Verification:**
```bash
<command>
```

**Recent log:**
```
```

---

### 4. <step name>  ⏳ not-started

**What:** <description>

**Verification:**
```bash
<command>
```

**Recent log:**
```
```

---

## Summary  *(filled in after all steps complete)*

<What was built. Where it lives. How to run it. What the user should do next.>

---

## How to use this template

1. Copy this file to `~/skill-workspace/plans/YYYY-MM-DD-<your-topic>.md`
   (use `mkdir -p ~/skill-workspace/plans` first if the dir doesn't exist).
2. Fill in the metadata block at top.
3. Write all your steps in the **Status Dashboard** table FIRST so the user
   sees the whole shape before any execution.
4. Expand each step into its **Steps (full detail)** section.
5. Hand off to `executing-plans`, which will:
   - Update each row's **Status** + **Started** / **Completed** times live.
   - Append shell/tool output to the **Recent log** block under each step
     while it's 🚧 in-progress.
   - Trim each Recent log to the last ~20 lines for readability.
   - Mark the top-level **Status:** field ✅ completed when done and write a
     final **## Summary** section.

The user can `cat` this file at ANY moment and see exactly where the agent
is, what was just done, and what remains. No silent work.
