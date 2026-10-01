"""browser_check — the dashboard, clicked by a real browser.

This is the one check in the repository that a server-side rendering test
cannot stand in for. The 0.4.1 defect is the proof: a `htmx:beforeSwap`
handler cancelled htmx's own swap and then called `htmx.swap()`, which does not
exist in htmx 1.x. Chrome and Edge support `document.startViewTransition`, so
they reached that line and every boosted link did nothing for a week. Firefox
and Safari returned earlier and were fine. Meanwhile the suite that "covers"
that code asserts on the *source text* of base.html — `assert "boosted" in js` —
and stayed green throughout.

So three things are non-negotiable here:

  · Chromium. A Firefox run would have been green through the whole outage.
  · htmx must actually be live before any click is judged. It is loaded from
    unpkg, and `hx-boost` is inert when the CDN does not answer — at which
    point every `<a href>` is an ordinary navigation that works perfectly and
    the test proves nothing. That is reported as BLOCKED, never as a pass.
  · Each click is judged on TWO things: that the URL moved and that the content
    changed. During the defect htmx fetched the page and then cancelled the
    swap, so the network looked healthy and the DOM did not move. A
    network-only assertion would have passed.

Any console error or uncaught page error fails the check and is quoted.

Usage: browser_check.py <base-url> <project> [<out.json>]
"""
from __future__ import annotations

import json
import re
import sys

OK, FAIL, BLOCKED = "OK", "FAIL", "BLOCKED"


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []
        self.console: list[str] = []
        self.env: dict = {}

    def add(self, verdict: str, label: str, detail: str = "") -> None:
        self.checks.append({"verdict": verdict, "label": label, "detail": detail})

    def json(self) -> dict:
        return {"checks": self.checks, "console": self.console, "env": self.env}


def _signature(page) -> str:
    """What is on the page now, reduced to something comparable. Used to prove
    the content changed — not just the address bar."""
    try:
        return page.evaluate("() => (document.body.innerText || '').slice(0, 4000)")
    except Exception:
        return ""


def run(base: str, project: str) -> Report:
    r = Report()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(args=["--no-sandbox"])
        except Exception as e:                       # no browser binary downloaded
            r.add(BLOCKED, "launch Chromium",
                  f"{type(e).__name__}: {e}. Run `python -m playwright install chromium`.")
            return r
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and r.console.append(f"console.error: {m.text}"))
        page.on("pageerror", lambda e: r.console.append(f"pageerror: {e}"))

        # ── preflight: is the client-side machinery actually live? ───────────
        page.goto(f"{base}/history", wait_until="load")
        page.wait_for_timeout(1200)                  # the CDN scripts
        r.env = {
            "htmx": page.evaluate("() => typeof window.htmx"),
            "htmx_version": page.evaluate("() => (window.htmx && window.htmx.version) || null"),
            "htmx_swap_fn": page.evaluate("() => typeof (window.htmx && window.htmx.swap)"),
            "startViewTransition": page.evaluate("() => typeof document.startViewTransition"),
            "hx_boost_on_body": page.evaluate("() => document.body.getAttribute('hx-boost')"),
            "user_agent": page.evaluate("() => navigator.userAgent"),
        }
        if r.env["htmx"] != "object":
            # Without htmx every link is a plain navigation that works, so
            # nothing below would test the thing this file exists to test.
            r.add(BLOCKED, "htmx is live (loaded from unpkg)",
                  "window.htmx is not an object — the CDN did not answer, so hx-boost is inert "
                  "and every click below would pass for the wrong reason. This check cannot run "
                  "offline.")
            browser.close()
            return r
        r.add(OK, f"htmx is live · v{r.env['htmx_version']} · hx-boost={r.env['hx_boost_on_body']}")
        if r.env["startViewTransition"] != "function":
            r.add(BLOCKED, "the browser supports document.startViewTransition",
                  "this Chromium does not, so the view-transition wrapper — the code the 0.4.1 "
                  "defect lived in — is never reached and the run proves nothing about it.")
        else:
            r.add(OK, "document.startViewTransition exists — the 0.4.1 code path is reachable")

        # ── 1 · click a task row ────────────────────────────────────────────
        rows = page.locator('ul li a[href^="/plan/"]')
        if rows.count() == 0:
            r.add(FAIL, "the task list has a clickable row",
                  "no <a href='/plan/…'> on /history — the ledger this ran against has no plans")
        else:
            row = rows.first
            want = row.get_attribute("href")
            before = _signature(page)
            row.click()
            try:
                page.wait_for_url(f"**{want}", timeout=8000)
                page.wait_for_selector("#dashboard-content", timeout=8000)
                moved = page.url.endswith(want)
                changed = _signature(page) != before
                r.add(OK if (moved and changed) else FAIL,
                      "click a task row -> the plan page",
                      f"url {'moved to ' + page.url if moved else 'did NOT move (still ' + page.url + ')'}; "
                      f"content {'changed' if changed else 'did NOT change'}")
            except Exception as e:
                r.add(FAIL, "click a task row -> the plan page",
                      f"the click did nothing: {type(e).__name__}: {str(e).splitlines()[0]} "
                      f"(url is still {page.url})")

        # ── 2 · click through to a node ─────────────────────────────────────
        # The graph table is the reliable source of node links; the plan page's
        # changed-by-history block needs influence rows that a small project may
        # not have.
        page.goto(f"{base}/graph/{project}?mode=story", wait_until="load")
        page.wait_for_timeout(600)
        links = page.locator('a[href^="/node/"]')
        if links.count() == 0:
            r.add(FAIL, "a node is reachable by clicking",
                  f"no <a href='/node/…'> on /graph/{project} — the graph table is clustered or "
                  "empty, so there is nothing to click through to")
        else:
            link = links.first
            want = link.get_attribute("href")
            before = _signature(page)
            link.click()
            try:
                page.wait_for_url("**/node/**", timeout=8000)
                page.wait_for_selector('[data-panel="node-head"]', timeout=8000)
                bad = page.locator('[data-state="unavailable"], [data-state="not-found"]')
                detail = f"url {page.url}; content {'changed' if _signature(page) != before else 'unchanged'}"
                if bad.count():
                    # a 200 page that says it has nothing — a status assertion misses this
                    r.add(FAIL, "click through to a node", f"the node page rendered a "
                          f"{bad.first.get_attribute('data-state')} state. {detail}")
                else:
                    head = page.locator('[data-panel="node-head"] h1').inner_text()
                    r.add(OK if _signature(page) != before else FAIL,
                          "click through to a node", f"landed on {head.strip()[:60]!r}. {detail}")
            except Exception as e:
                r.add(FAIL, "click through to a node",
                      f"the click did nothing ({want}): {type(e).__name__}: "
                      f"{str(e).splitlines()[0]} (url is still {page.url})")

        # ── 3 · switch a graph view ─────────────────────────────────────────
        # The chip that is current renders as <span data-current="1"> and the
        # others as <a>. Which one carries data-current is server-rendered AND
        # requires the boosted swap to have landed, so it is the cleanest
        # possible "the click did something" assertion.
        page.goto(f"{base}/graph/{project}", wait_until="load")
        page.wait_for_timeout(600)
        was = page.locator("[data-mode-chip][data-current='1']")
        start_mode = was.first.get_attribute("data-mode-chip") if was.count() else None
        target = next((m for m in ("data", "full", "story")
                       if m != start_mode and page.locator(f"a[data-mode-chip='{m}']").count()), None)
        if not target:
            r.add(FAIL, "switch the graph view",
                  f"no other mode chip is a link (current mode: {start_mode})")
        else:
            before = _signature(page)
            page.locator(f"a[data-mode-chip='{target}']").click()
            try:
                page.wait_for_url(f"**mode={target}*", timeout=8000)
                page.wait_for_selector(f"[data-mode-chip='{target}'][data-current='1']", timeout=8000)
                stale = page.locator(f"[data-mode-chip='{start_mode}'][data-current='1']").count()
                r.add(OK if (stale == 0 and _signature(page) != before) else FAIL,
                      f"switch the graph view {start_mode} -> {target}",
                      f"url {page.url}; the {target} chip is now current"
                      + ("" if stale == 0 else f"; but {start_mode} still claims to be current"))
            except Exception as e:
                r.add(FAIL, f"switch the graph view {start_mode} -> {target}",
                      f"the click did nothing: {type(e).__name__}: {str(e).splitlines()[0]} "
                      f"(url is still {page.url})")

        # ── 4 · open the ask page from the view bar ──────────────────────────
        page.goto(f"{base}/graph/{project}", wait_until="load")
        page.wait_for_timeout(600)
        ask = page.locator('a[data-view-link="ledger"]')
        if ask.count() == 0:
            disabled = page.locator('[data-view-link="ledger"][data-view-disabled="1"]').count()
            r.add(FAIL, "open the ask page from the view bar",
                  "the Ask chip is a disabled <span>, not a link — the context triple carries no "
                  "project" if disabled else "there is no Ask chip in the view bar at all")
        else:
            before = _signature(page)
            ask.click()
            try:
                page.wait_for_url("**/ledger?*", timeout=8000)
                page.wait_for_selector('[data-panel="ledger"]', timeout=8000)
                r.add(OK if _signature(page) != before else FAIL,
                      "open the ask page from the view bar",
                      f"url {page.url}; content "
                      f"{'changed' if _signature(page) != before else 'did NOT change'}")
            except Exception as e:
                r.add(FAIL, "open the ask page from the view bar",
                      f"the click did nothing: {type(e).__name__}: {str(e).splitlines()[0]} "
                      f"(url is still {page.url})")

        # ── 4b · the one hx-push-url in the codebase ────────────────────────
        # The ask form replaces #ledger-results only and pushes the query into
        # the address bar. Pure client-side: no server test sees it.
        page.goto(f"{base}/ledger?project={project}", wait_until="load")
        page.wait_for_timeout(600)
        form = page.locator('form[hx-get="/ledger/results"]')
        if form.count() == 0:
            r.add(FAIL, "the ask form submits and pushes its query into the URL",
                  "no form[hx-get='/ledger/results'] on the ask page")
        else:
            before = _signature(page)
            page.fill('form[hx-get="/ledger/results"] input[name="q"]', "why was discount_rate changed")
            page.locator('form[hx-get="/ledger/results"] button').first.click()
            try:
                page.wait_for_url("**q=why*", timeout=15000)
                page.wait_for_timeout(800)
                r.add(OK if _signature(page) != before else FAIL,
                      "the ask form submits and pushes its query into the URL",
                      f"url {page.url}")
            except Exception as e:
                r.add(FAIL, "the ask form submits and pushes its query into the URL",
                      f"hx-push-url did not fire: {type(e).__name__}: {str(e).splitlines()[0]} "
                      f"(url is still {page.url})")

        browser.close()

    # ── console: any error at all is a failure, and it is quoted ────────────
    noise = re.compile(r"cdn\.tailwindcss\.com should not be used in production", re.I)
    real = [c for c in r.console if not noise.search(c)]
    if real:
        r.add(FAIL, f"no console errors ({len(real)} seen)", "\n".join(dict.fromkeys(real))[:1500])
    else:
        r.add(OK, "no console errors")
    return r


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: browser_check.py <base-url> <project> [out.json]", file=sys.stderr)
        return 64
    try:
        rep = run(argv[1], argv[2])
    except Exception as e:                            # the harness must still get a verdict
        rep = Report()
        rep.add(BLOCKED, "drive the dashboard with a real browser", f"{type(e).__name__}: {e}")
    doc = rep.json()
    if len(argv) > 3:
        with open(argv[3], "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
    print(json.dumps(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
