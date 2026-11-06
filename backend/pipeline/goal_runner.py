"""Goal mode: run a TEST CASE instead of a click-by-click script.

A test case says WHAT to achieve and WHAT must be true at the end:

    Search for Pizza, open Margherita Pizza and verify its name and price are displayed.
    Test data: email=demo@x.com; password=Passw0rd!
    Expected: The selected product's name and price are displayed correctly.

The runner is a plan -> act -> observe loop:
  1. observe : read the screen (UI tree + screenshot)
  2. plan    : ask the LLM for ONE next action (tap / type / scroll / back / wait / done / fail)
  3. act     : execute it with the same executor script mode uses (run_agent_steps), so label
               matching, keyboard handling, scrolling and the safety checks are shared
  4. judge   : when the planner says "done", compare the FINAL screen with the expected results

Safety rules that do not depend on the LLM behaving:
  * a value may only be typed if it comes from the test data, the test case text, or is visible
    on screen (e.g. a demo-credentials hint). Anything else is rejected, never typed.
  * repeating the same action 3 times, or 4 failed actions in a row, stops the run (FAILED).
  * a step limit (VEROFLOW_MAX_STEPS, default 40) stops the run (FAILED).
  * expected results written in "double quotes" are checked as exact text on the final screen;
    the others are judged by the LLM and are reported as such.
"""
import os
import re
import json
import time

from pipeline.agent_runner import (
    run_agent_steps, describe_elements, wait_stable, content_sig, hide_keyboard,
    unexpected_errors, _norm, recognize, screen_name_of,
)
PLANNER_RAG = os.environ.get("VEROFLOW_PLANNER_RAG", "1") != "0"

MAX_STEPS = int(os.environ.get("VEROFLOW_MAX_STEPS", "40"))
ACTIONS = ("tap", "type", "scroll", "back", "wait", "done", "fail")
QUOTED = re.compile(r'["“`]([^"”`]{2,})["”`]')


# ---------------------------------------------------------------- parsing the test case
def _split_items(s):
    out = []
    for part in re.split(r"[\n;|]", s or ""):
        part = part.strip().lstrip("-*• ").strip()
        if part:
            out.append(part)
    return out


def _parse_data(items):
    data = {}
    for it in items:
        m = re.match(r"^\s*([^=:]+?)\s*[=:]\s*(.+?)\s*$", it)
        if m:
            data[m.group(1).strip()] = m.group(2).strip()
    return data


def load_base_data():
    """App-wide test data (e.g. the demo account), written once so no test case has to repeat it."""
    path = os.environ.get("VEROFLOW_TEST_DATA", "test_data.json")
    try:
        with open(path) as f:
            d = json.load(f)
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:
        return {}


PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
ID_RE = re.compile(r"^(?:run\s+|execute\s+)?([A-Za-z]{2,}[-_ ]?\d+[A-Za-z0-9_-]*)\.?$", re.I)


def _norm_id(i):
    return re.sub(r"[\s_]+", "-", str(i).strip().lower())


def load_case_library():
    """Stored test cases (id, goal, expected, optional test_data) from test_cases.json, so the
    dashboard box only needs the ID, e.g. 'TC-03'. The expected result is written ONCE, there."""
    path = os.environ.get("VEROFLOW_TEST_CASES", "test_cases.json")
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return {}
    items = d if isinstance(d, list) else d.get("test_cases", [])
    return {_norm_id(c["id"]): c for c in items if isinstance(c, dict) and c.get("id")}


def case_id_of(text):
    m = ID_RE.match((text or "").strip())
    return _norm_id(m.group(1)) if m else None


def looks_like_test_case(text):
    t = (text or "").strip()
    if case_id_of(t):
        return True
    if t.startswith("{"):
        try:
            d = json.loads(t)
            return isinstance(d, dict) and ("goal" in d or "test_case" in d)
        except Exception:
            return False
    return any(re.match(r"^\s*expected", ln, re.I) for ln in t.splitlines())


def parse_test_case(text):
    """-> {"title","goal","data","expected"} from JSON or from the plain 3-line format."""
    text = (text or "").strip()
    cid = case_id_of(text)
    if cid:
        lib = load_case_library()
        if cid not in lib:
            known = ", ".join(sorted(k.upper() for k in lib)) or "none (test_cases.json not found in the backend folder)"
            raise ValueError(f"Test case '{text}' was not found in test_cases.json. Known test cases: {known}")
        text = json.dumps(lib[cid])
    title, goal, data, expected, setup = "", "", {}, [], ""
    if text.startswith("{"):
        try:
            d = json.loads(text)
            title = str(d.get("title") or d.get("id") or "")
            goal = str(d.get("goal") or d.get("test_case") or "")
            setup = str(d.get("precondition") or d.get("setup") or "")
            data = {str(k): str(v) for k, v in (d.get("test_data") or d.get("data") or {}).items()}
            e = d.get("expected") or d.get("expected_result") or []
            expected = _split_items(e) if isinstance(e, str) else [str(x) for x in e]
        except Exception:
            pass
    if not goal:
        mode, goal_lines, data_items, exp_items, setup_lines = "goal", [], [], [], []
        for line in text.splitlines():
            m_exp = re.match(r"^\s*expected(?:\s+results?)?\s*[:\-]\s*(.*)$", line, re.I)
            m_dat = re.match(r"^\s*(?:test\s+)?data\s*[:\-]\s*(.*)$", line, re.I)
            m_tc = re.match(r"^\s*test\s*case\s*[:\-]\s*(.*)$", line, re.I)
            m_pre = re.match(r"^\s*(?:pre-?conditions?|setup)\s*[:\-]\s*(.*)$", line, re.I)
            if m_pre:                      # one line; the lines after it still belong to the goal
                mode = "goal"
                setup_lines.append(m_pre.group(1))
            elif m_exp:
                mode = "exp"
                exp_items.append(m_exp.group(1))
            elif m_dat:
                mode = "data"
                data_items.append(m_dat.group(1))
            elif m_tc:
                mode = "goal"
                goal_lines.append(m_tc.group(1))
            elif mode == "exp":
                exp_items.append(line)
            elif mode == "data":
                data_items.append(line)
            else:
                goal_lines.append(line)
        if goal_lines and re.match(r"^\s*[A-Za-z]{2,}[-_ ]?\d+[A-Za-z0-9_-]*\s*$", goal_lines[0]):
            title = goal_lines[0].strip()
            goal_lines = goal_lines[1:]
        goal = " ".join(l.strip() for l in goal_lines if l.strip())
        setup = " ".join(l.strip() for l in setup_lines if l.strip())
        data = _parse_data(_split_items("\n".join(data_items)))
        expected = _split_items("\n".join(exp_items))
    merged = load_base_data()
    merged.update(data)

    def fill(t):
        missing = [k for k in PLACEHOLDER.findall(t) if k not in merged]
        if missing:
            raise ValueError(f"Test case {title or ''} uses {{{missing[0]}}} but test_data.json / test_data "
                             f"has no '{missing[0]}'. Known keys: {sorted(merged)}")
        return PLACEHOLDER.sub(lambda m: merged[m.group(1)], t)
    return {"title": title, "goal": fill(goal), "data": merged,
            "expected": [fill(e) for e in expected], "setup": fill(setup)}


def goal_actions(text):
    """Shape the test case like parsed_actions so the dashboard can show it."""
    c = parse_test_case(text)
    acts = ([{"action": "setup", "target": c["setup"]}] if c["setup"] else []) + [{"action": "goal", "target": c["goal"]}]
    acts += [{"action": "expect", "target": e} for e in c["expected"]]
    return acts


# ---------------------------------------------------------------- planner
def _mask(data):
    return {k: ("***" if re.search(r"pass|pwd|secret|cvv|card", k, re.I) else v) for k, v in data.items()}


def _screen_lines(nodes):
    lines = []
    for n, label, kind in describe_elements(nodes, limit=60):
        flags = []
        if n.get("clickable"):
            flags.append("clickable")
        if n.get("checked"):
            flags.append("checked")
        if n.get("selected"):
            flags.append("selected")
        if not n.get("enabled", True):
            flags.append("disabled")
        lines.append(f"- {label} [{kind}{', ' + ', '.join(flags) if flags else ''}]")
    return lines


def _prompt(case, history, nodes, note="", screen=None):
    data_txt = "\n".join(f"- {k}: {v}" for k, v in case["data"].items()) or "(none given)"
    exp_txt = "\n".join(f"- {e}" for e in case["expected"]) or "(none given)"
    hist = "\n".join(history[-10:]) or "(nothing done yet)"
    return (
        "You are an Android app tester. Reach the GOAL by choosing ONE next action at a time.\n\n"
        + (f"PRECONDITION (do this first; skip it if it is already satisfied, e.g. you are already logged in): {case['setup']}\n\n" if case.get("setup") else "")
        + f"GOAL: {case['goal']}\n\nTEST DATA (the only values you may type):\n{data_txt}\n\n"
        f"EXPECTED RESULT (what the final screen must show):\n{exp_txt}\n\n"
        f"ACTIONS SO FAR:\n{hist}\n{note}\n"
        + (f"CURRENT SCREEN (from screen memory, {screen['source']}"
           + (f", similarity {screen['similarity']}" if screen.get('similarity') is not None else "")
           + f"): {screen['name']}\n" if screen else "")
        + "ELEMENTS ON THE CURRENT SCREEN (label [type]):\n" + "\n".join(_screen_lines(nodes)) + "\n\n"
        "Rules:\n"
        "- target must be copied EXACTLY from a label in the list above (for scroll use 'down' or 'up').\n"
        "- type: value must come from TEST DATA or from text that is visible on screen (such as a demo-account hint). "
        "Never invent a value. If you need a value you do not have, answer fail with reason 'missing test data: <what>'.\n"
        "- Prefer the most direct path. Do not repeat an action that already failed twice.\n"
        "- Before answering done, make sure the CURRENT screen shows the evidence for the EXPECTED RESULT "
        "(navigate there first, e.g. open the cart or the orders list). Then answer done.\n"
        "- If the goal cannot be reached from this app, answer fail with the reason.\n"
        "- Do ONLY what the GOAL says. Never take extra actions just to make the EXPECTED RESULT appear "
        "(for example pressing Back after a login unexpectedly succeeded). When the goal's actions are done, "
        "answer done even if the screen differs from the expected result: the judge decides pass or fail.\n\n"
        'Return ONLY JSON: {"action": "tap|type|scroll|back|wait|done|fail", "target": "...", "value": "...", "reason": "..."}'
    )


def _plan(case, history, nodes, png, note="", screen=None):
    from pipeline.llm_client import chat_json
    last = None
    for _ in range(2):
        try:
            d = chat_json(_prompt(case, history, nodes, note, screen), png)
            a = str(d.get("action", "")).lower().strip()
            if a in ACTIONS:
                return {"action": a, "target": str(d.get("target") or "").strip(),
                        "value": str(d.get("value") or ""), "reason": str(d.get("reason") or "")}
            last = f"planner returned an unknown action: {d!r}"[:200]
        except Exception as e:
            last = f"planner LLM error: {' '.join(str(e).split())[:200]}"
    raise RuntimeError(last)


def value_allowed(value, case, nodes):
    """A typed value must come from the test data, the test case text, or be visible on screen."""
    v = _norm(value)
    if not v:
        return True
    if any(v == _norm(x) for x in case["data"].values()):
        return True
    if v in _norm(case["goal"] + " " + case.get("setup", "")):
        return True
    seen = _norm(" ".join(t for n in nodes for t in (n.get("text", ""), n.get("desc", "")) if t))
    return v in seen


# ---------------------------------------------------------------- judge
NEGATIVE = re.compile(r'^\s*(?:not|no|must\s+not\s+(?:show|appear|be\s+(?:shown|visible|displayed))|absent)\s*[:\-]?\s*["“`]', re.I)


def _words(t):
    return re.findall(r"[a-z0-9]+", str(t).lower())


def text_on_screen(q, texts):
    """True when the words of q appear, in order and as whole words, inside ONE element's text.
    (Joining all texts and searching substrings let "Paid" match "Unpaid".)"""
    qw = _words(q)
    if not qw:
        return False
    for t in texts:
        tw = _words(t)
        for k in range(len(tw) - len(qw) + 1):
            if tw[k:k + len(qw)] == qw:
                return True
    return False


def judge(driver, case, history, run_dir, trace):
    """Compare the final screen with the expected results. Returns (all_met, items, errors)."""
    from pipeline.step_runner import visible_nodes
    nodes = visible_nodes(driver)
    texts = [t for n in nodes for t in (n["text"], n["desc"]) if t]
    seen = _norm(" ".join(texts)).lower()
    png = f"{run_dir}/final_judge.png"
    try:
        driver.get_screenshot_as_file(png)
    except Exception:
        png = None
    items, pending = [], []
    for idx, e in enumerate(case["expected"]):
        quoted = QUOTED.findall(e)
        if quoted and NEGATIVE.match(e):
            present = [q for q in quoted if text_on_screen(q, texts)]
            items.append({"expected": e, "method": "exact text (must be absent)", "met": not present,
                          "evidence": ("absent: " + ", ".join(repr(q) for q in quoted)) if not present
                          else "unexpectedly on screen: " + ", ".join(repr(q) for q in present)})
        elif quoted:
            missing = [q for q in quoted if not text_on_screen(q, texts)]
            items.append({"expected": e, "method": "exact text", "met": not missing,
                          "evidence": ("found " + ", ".join(repr(q) for q in quoted)) if not missing
                          else "not on screen: " + ", ".join(repr(q) for q in missing)})
        else:
            items.append({"expected": e, "method": "LLM judge", "met": None, "evidence": ""})
            pending.append(idx)
    if pending:
        try:
            from pipeline.llm_client import chat_json
            # Number the items 1..n among the ones the judge sees (NOT their position in the whole list):
            # a model copies the example's number, so absolute positions made it answer for the wrong item.
            lst = "\n".join(f"{k}: {case['expected'][i]}" for k, i in enumerate(pending, 1))
            prompt = (
                "You are the test oracle for an Android app test. Decide, for each expected result, whether the "
                "FINAL screen satisfies it. Judge only what the screen shows (text list and screenshot); "
                "do not assume. Be strict. Some expected results describe an earlier step of the flow (for example "
                "'search results should appear'): count those as met only if the Last actions show that step was done "
                "successfully and the final screen does not contradict it.\n\n"
                f"Test case goal: {case['goal']}\n"
                f"Last actions: {'; '.join(history[-6:])}\n"
                f"Text visible on the final screen: {texts[:80]}\n\n"
                f"Expected results:\n{lst}\n\n"
                'Return ONLY JSON with one entry per numbered expected result, where "index" is the number in front of it: '
                '{"results": [{"index": 1, "met": true, "evidence": "short reason"}]}'
            )
            res = [r for r in chat_json(prompt, png).get("results", []) if isinstance(r, dict)]
            by = {}
            for r in res:
                try:
                    by[int(r.get("index", -1))] = r
                except (TypeError, ValueError):
                    pass
            for k, i in enumerate(pending, 1):
                r = by.get(k)
                if r is None and len(res) == len(pending):
                    r = res[k - 1]        # same number of answers as questions: pair them by order
                if r is None:
                    items[i].update(met=False, evidence="the judge gave no usable answer for this item "
                                                        f"(it returned: {str(res)[:120]})")
                else:
                    items[i].update(met=bool(r.get("met")), evidence=str(r.get("evidence", ""))[:200])
        except Exception as ex:
            for i in pending:
                items[i].update(met=False, evidence="could not be judged: " + " ".join(str(ex).split())[:150])
    verifies = [{"action": "verify", "target": q} for e in case["expected"] if not NEGATIVE.match(e)
                for q in QUOTED.findall(e)]
    errors = unexpected_errors(nodes, verifies)
    for it in items:
        trace.append(f"    # expected: {it['expected']!r} -> {'MET' if it['met'] else 'NOT MET'} "
                     f"[{it['method']}] {it['evidence']}")
    return all(it["met"] for it in items), items, errors


# ---------------------------------------------------------------- main loop
def run_goal_steps(driver, text, run_dir, trace):
    from pipeline.step_runner import visible_nodes
    case = parse_test_case(text)
    if not case["goal"]:
        raise RuntimeError("Goal mode: the test case has no goal text.")
    state = {"steps": [], "screens": [], "history": []}
    trace.append("# Generated by VeroFlow GOAL mode: test case -> plan/act/observe loop -> expected-result check")
    trace.append(f"# goal: {case['goal']}")
    trace.append(f"# test data keys: {sorted(case['data'])}")
    trace.append("from appium.webdriver.common.appiumby import AppiumBy")
    trace.append("")
    trace.append("def run(driver):")
    hist, recent, fails_in_row, rejects, verdict_error = [], [], 0, 0, None
    outcome = {"goal": case["goal"], "title": case["title"], "test_data": _mask(case["data"]),
               "expected": case["expected"], "verdict": "FAILED", "reason": "", "iterations": 0, "checks": []}
    prev_png, note, last_labels, empty_in_row = None, "", [], 0
    last_ok, blocked = None, 0     # (action key, screen content after it) of the last successful action

    def ctx():
        return (f" Last actions: {'; '.join(hist[-5:]) or 'none'}. "
                f"Screen at that point: {last_labels[:25]}")
    try:
        time.sleep(2)
        for it in range(1, MAX_STEPS + 1):
            outcome["iterations"] = it
            hide_keyboard(driver)
            nodes = wait_stable(driver)
            if not nodes:
                # Nothing readable: a system overlay on top, a screen without accessibility nodes, or the
                # UiAutomator2 server failing to dump the screen. Try Back twice, then stop with the REAL
                # reason instead of burning the whole step limit (wait_stable hides the exception).
                empty_in_row += 1
                try:
                    visible_nodes(driver)
                    why = "the UI tree is empty (a system overlay, or a screen with no accessibility nodes)"
                except Exception as e:
                    why = "reading the screen failed: " + " ".join(str(e).split())[:300]
                trace.append(f"    # screen {it}: unreadable ({why[:120]})")
                if empty_in_row >= 3:
                    outcome["reason"] = "screen unreadable"
                    raise RuntimeError("Goal mode: cannot read the screen (3 attempts in a row): " + why + ctx())
                try:
                    driver.back()
                except Exception:
                    pass
                time.sleep(1.5)
                continue
            empty_in_row = 0
            last_labels = [lbl for _, lbl, _ in describe_elements(nodes, 25)]
            png = f"{run_dir}/plan_{it}.png"          # kept as evidence; stored in screen memory on a miss
            try:
                driver.get_screenshot_as_file(png)
            except Exception:
                png = None
            screen = None
            if png and PLANNER_RAG:
                label, source, sim = recognize(png, nodes)
                screen = {"name": screen_name_of(label), "source": source, "similarity": sim}
                outcome.setdefault("planner_screens", []).append({"iteration": it, "screenshot": f"plan_{it}.png", **screen})
                trace.append(f"    # screen {it}: {screen['name']} ({source}"
                             + (f", similarity {sim}" if sim is not None else "") + ")")
            plan = _plan(case, hist, nodes, png, note, screen)
            note = ""
            a, target, value = plan["action"], plan["target"], plan["value"]
            trace.append(f"    # plan {it}: {a} {target!r} {('value=' + repr('***' if re.search('pass|cvv', target, re.I) else value)) if value else ''} -- {plan['reason'][:120]}")
            if a == "done":
                outcome["reason"] = "planner reported the goal as reached"
                break
            if a == "fail":
                reason = plan["reason"] or "planner could not reach the goal"
                exact_only = case["expected"] and all(QUOTED.findall(e) for e in case["expected"])
                if exact_only:
                    met, items, errors = judge(driver, case, hist, run_dir, trace)
                    outcome["checks"] = items
                    if met and not errors:
                        trace.append(f"    # planner said fail ({reason[:100]}), but every exact-text "
                                     "expected result is met on the final screen -> verdict from evidence")
                        outcome["reason"] = "expected results met (exact text); planner's 'fail' overruled"
                        break
                    bad = [f"'{i['expected']}' ({i['evidence']})" for i in items if not i["met"]]
                    outcome["reason"] = reason
                    raise RuntimeError(f"Goal mode: the agent stopped: {reason}. Evidence on the final screen: "
                                       + ("; ".join(bad) if bad else "; ".join(errors[:6])))
                outcome["reason"] = reason
                raise RuntimeError(f"Goal mode: the agent stopped: {reason} "
                                   "(judged by the planner LLM: quote the expected results to check them as exact text)")
            if a == "type" and not value_allowed(value, case, nodes):
                rejects += 1
                note = (f"\nYour last answer was REJECTED: the value {value!r} is not in the test data and not on screen. "
                        "Use only TEST DATA values, or answer fail with 'missing test data'.")
                hist.append(f"type {target} -> REJECTED (value not in test data)")
                if rejects >= 2:
                    outcome["reason"] = f"missing test data for '{target}'"
                    raise RuntimeError(f"Goal mode: missing test data for '{target}'. "
                                       f"The agent tried to type a value that is not in the test case.")
                continue
            key = (a, target.lower(), value)
            if a == "tap" and last_ok is not None and last_ok[0] == key and last_ok[1] == content_sig(nodes):
                # The same tap already succeeded and the screen has not changed since. Repeating it would
                # add the item again (e.g. ADD TO CART twice), so skip it and tell the planner.
                blocked += 1
                hist.append(f"tap {target} -> SKIPPED: this tap already succeeded; the screen does not change "
                            "for this button. Do the NEXT part of the goal.")
                note = (f"\nYour last answer repeated '{a} {target}', which ALREADY SUCCEEDED. It was not executed "
                        "again. Choose the next action of the goal (for example navigate to the next screen).")
                trace.append(f"    # plan {it}: repeated successful tap {target!r} skipped ({blocked})")
                if blocked >= 3:
                    outcome["reason"] = f"stuck: kept repeating the successful action '{a} {target}'"
                    raise RuntimeError(f"Goal mode: stuck, the agent kept asking to repeat '{a} {target}', which had "
                                       "already succeeded (it was executed only once)." + ctx())
                continue
            recent.append(key)
            if len(recent) >= 3 and recent[-1] == recent[-2] == recent[-3]:
                outcome["reason"] = f"stuck repeating: {a} {target}"
                raise RuntimeError(f"Goal mode: stuck, the agent repeated '{a} {target}' 3 times without progress." + ctx())
            act = {"action": a, "target": target}
            if a == "type":
                act["value"] = value
            try:
                run_agent_steps(driver, [act], run_dir, trace, state=state, final_scan=False, use_llm=False)
                fails_in_row = 0
                shown = "***" if (a == "type" and re.search("pass|cvv|card", target, re.I)) else value
                appeared = state["steps"][-1].get("appeared") if state["steps"] else None
                hist.append(f"{a} {target}{(' = ' + shown) if value else ''} -> ok"
                            + (f" (briefly showed: {', '.join(repr(t) for t in appeared)})" if appeared else ""))
                try:
                    last_ok = (key, content_sig(wait_stable(driver, 3)))
                except Exception:
                    last_ok = None
            except Exception as e:
                fails_in_row += 1
                msg = " ".join(str(e).split())[:200]
                hist.append(f"{a} {target} -> FAILED: {msg}")
                note = f"\nYour last action FAILED: {msg}\nChoose a different action."
                if fails_in_row >= 4:
                    outcome["reason"] = "4 failed actions in a row"
                    raise RuntimeError(f"Goal mode: 4 actions failed in a row. Last error: {msg}" + ctx())
        else:
            outcome["reason"] = f"step limit ({MAX_STEPS}) reached"
            raise RuntimeError(f"Goal mode: the goal was not reached within {MAX_STEPS} planning steps." + ctx())

        if outcome["checks"]:
            met, items, errors = True, outcome["checks"], []
        else:
            met, items, errors = judge(driver, case, hist, run_dir, trace)
        outcome["checks"] = items
        if not case["expected"]:
            trace.append("    # NOTE: no expected result was given, so the verdict only means the agent says the goal is reached")
        bad = [f"'{i['expected']}' ({i['evidence']})" for i in items if not i["met"]]
        if bad:
            outcome["reason"] = "expected result not met"
            raise RuntimeError("Goal reached, but the expected result is NOT met: " + "; ".join(bad))
        # A negative test (e.g. empty checkout form) EXPECTS a validation message. If the Expected list asks
        # for one and it was found, the errors on screen are the expected result, not a failure.
        expects_error = any(
            i["met"] and "absent" not in str(i.get("method", ""))
            and re.search(r"required|invalid|error|incorrect|wrong|must|not allowed|empty|failed", str(i["expected"]), re.I)
            for i in items)
        if errors and expects_error:
            trace.append("    # NOTE: validation errors on screen are EXPECTED by this test case: " + "; ".join(errors[:6]))
            outcome["expected_validation_errors"] = errors[:6]
            errors = []
        if errors:
            outcome["reason"] = "validation errors on the final screen"
            raise RuntimeError("Goal reached, but the final screen shows validation errors: " + "; ".join(errors[:6]))
        outcome["verdict"] = "PASSED"
    except Exception as e:
        verdict_error = e
        if not outcome["reason"]:
            outcome["reason"] = str(e)[:200]
    finally:
        for fname, data in (("steps.json", state["steps"]), ("screens.json", state["screens"]), ("goal_result.json", outcome)):
            try:
                with open(f"{run_dir}/{fname}", "w") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
        try:
            os.remove(f"{run_dir}/plan_current.png")
        except Exception:
            pass
    if verdict_error is not None:
        raise verdict_error
    return trace


# ---------------------------------------------------------------- metrics (computed, never typed in)
def compute_metrics(run_dir, mode, verdict):
    """Run metrics computed from the run's own evidence (steps.json, goal_result.json).
    Written to metrics.json so every number in a report can be traced back to a file."""
    def load(name, default):
        try:
            with open(f"{run_dir}/{name}") as f:
                return json.load(f)
        except Exception:
            return default

    steps = load("steps.json", [])
    goal = load("goal_result.json", {})
    n = len(steps)
    passed = sum(1 for s in steps if s.get("status") == "passed")
    # assertions = what the test claims must be true: `verify` steps (script mode) or expected results (goal mode)
    if mode == "goal":
        checks = goal.get("checks", [])
        a_total = len(checks)
        a_met = sum(1 for c in checks if c.get("met"))
        exact = sum(1 for c in checks if c.get("method") == "exact text")
        judged = sum(1 for c in checks if c.get("method") == "LLM judge")
    else:
        verifies = [s for s in steps if s.get("action") in ("verify", "assert", "check", "confirm", "expect")]
        a_total, a_met = len(verifies), sum(1 for s in verifies if s.get("status") == "passed")
        exact, judged = a_total, 0
    screens = [s for s in steps if s.get("screen_source")]
    hits = sum(1 for s in screens if "memory" in str(s.get("screen_source", "")).lower())
    rate = lambda a, b: round(a / b, 3) if b else None
    return {
        "mode": mode,
        "verdict": verdict,
        "steps_executed": n,
        "steps_passed": passed,
        "steps_failed": n - passed,            # in goal mode: attempts the agent recovered from (or the last one)
        "step_pass_rate": rate(passed, n),
        "assertions_total": a_total,
        "assertions_met": a_met,
        "assertion_pass_rate": rate(a_met, a_total),
        "assertions_exact_text": exact,
        "assertions_llm_judged": judged,
        "planner_calls": goal.get("iterations") if mode == "goal" else None,
        "steps_resolved_by_llm": sum(1 for s in steps if str(s.get("decided_by", "")).strip().lower().startswith("llm")),
        "screens_recognized": len(screens),
        "screen_memory_hits": hits,
        "screen_memory_hit_rate": rate(hits, len(screens)),
    }