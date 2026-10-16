import os
import json
import time

from pipeline.step_runner import (
    visible_nodes, best_node, label_score, target_tokens,
    toggle_in_row, is_toggle, tap_node, TYPE_WORDS, VERIFY_WORDS,
)



ICONISH = ("icon", "arrow", "button", "image", "logo", "menu", "toggle", "switch", "checkbox", "radio", "tab")


def node_label(n):
    return n["text"] or n["desc"] or n["rid"]


def node_score(tt, n):
    if n is None:
        return 0
    return max(label_score(tt, n["text"]), label_score(tt, n["desc"]), label_score(tt, n["rid"]))


def screen_name_of(label):
    for line in (label or "").splitlines():
        if line.lower().startswith("screen name"):
            return line.split(":", 1)[-1].strip()
    first = (label or "").strip().split("\n")[0][:60]
    return first or "Unknown screen"


def nearest_text(nodes, ctrl):
    inside = [n for n in nodes
              if n["text"] and ctrl["x1"] <= (n["x1"] + n["x2"]) // 2 <= ctrl["x2"]
              and ctrl["y1"] <= (n["y1"] + n["y2"]) // 2 <= ctrl["y2"]]
    if inside:
        return min(inside, key=lambda n: n["x1"])["text"]
    cy = (ctrl["y1"] + ctrl["y2"]) // 2
    best, best_d = None, None
    for n in nodes:
        if not n["text"]:
            continue
        ncy = (n["y1"] + n["y2"]) // 2
        if abs(ncy - cy) <= 60:
            d = abs(n["x1"] - ctrl["x2"])
            if best_d is None or d < best_d:
                best, best_d = n["text"], d
    return best


def describe_elements(nodes, limit=40):
    items, seen = [], set()
    for n in nodes:
        label = node_label(n)
        kind = n["cls"].split(".")[-1]
        if not label and is_toggle(n):
            near = nearest_text(nodes, n)
            label = f"control next to '{near}'" if near else ""
        if not label:
            continue
        key = (label, n["y1"], n["x1"])
        if key in seen:
            continue
        seen.add(key)
        items.append((n, label, kind))
    items.sort(key=lambda t: (t[0]["y1"], t[0]["x1"]))
    return items[:limit]


def signature(nodes):
    return tuple((n["text"], n["desc"], n["x1"], n["y1"]) for n in nodes)


def wait_stable(driver, max_wait=6):
    prev, nodes = None, []
    for _ in range(max_wait):
        try:
            nodes = visible_nodes(driver)
        except Exception:
            nodes = []
        sig = signature(nodes)
        if sig and sig == prev:
            break
        prev = sig
        time.sleep(1)
    return nodes


def keyboard_shown(driver):
    """Appium's own check can say False on Flutter apps, so also ask Android itself."""
    try:
        if driver.is_keyboard_shown():
            return True
    except Exception:
        pass
    try:
        import subprocess
        caps = getattr(driver, "capabilities", {}) or {}
        udid = caps.get("deviceUDID") or caps.get("udid") or os.environ.get("ANDROID_SERIAL") or ""
        cmd = ["adb"] + (["-s", str(udid)] if udid else []) + ["shell", "dumpsys", "input_method"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        return "mInputShown=true" in out or "isInputViewShown=true" in out
    except Exception:
        return False


def adb_cmd(driver, args, timeout=15):
    """Run `adb -s <device> <args>` straight from the backend."""
    import subprocess
    caps = getattr(driver, "capabilities", {}) or {}
    udid = caps.get("deviceUDID") or caps.get("udid") or os.environ.get("ANDROID_SERIAL") or ""
    cmd = ["adb"] + (["-s", str(udid)] if udid else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def keyboard_shown(driver):
    """Appium's is_keyboard_shown() can say 'no' while the keyboard is up, so also ask Android."""
    try:
        if driver.is_keyboard_shown():
            return True
    except Exception:
        pass
    try:
        out = adb_cmd(driver, ["shell", "dumpsys", "input_method"]).stdout
        return "mInputShown=true" in out or "isInputViewShown=true" in out
    except Exception:
        return False


def hide_keyboard(driver):
    """A keyboard left open covers the fields below it (taps on them hit the keys and type
    letters) and turns swipes into glide typing. Close it, and confirm that it closed."""
    hid = False
    for attempt in range(3):
        if not keyboard_shown(driver):
            return hid
        try:
            if attempt == 0:
                driver.hide_keyboard()
            else:
                adb_cmd(driver, ["shell", "input", "keyevent", "4"])  # Back closes the keyboard first
        except Exception:
            try:
                adb_cmd(driver, ["shell", "input", "keyevent", "4"])
            except Exception:
                pass
        hid = True
        time.sleep(0.8)
    return hid


def swipe(driver, finger):
    hide_keyboard(driver)
    size = driver.get_window_size()
    w, h = size["width"], size["height"]
    params = {"left": w // 4, "top": h // 4, "width": w // 2, "height": h // 2,
              "direction": finger, "percent": 0.75}
    driver.execute_script("mobile: swipeGesture", params)
    return params


def scroll_find(driver, tt, nodes, max_swipes=6):
    """Target not on screen: lists only expose the visible rows in the UI tree, so swipe
    down step by step until a label matches or the screen stops changing (end of list).
    Returns (node, nodes, swipes_done)."""
    prev = signature(nodes)
    for k in range(1, max_swipes + 1):
        swipe(driver, "up")  # finger up = content scrolls down
        time.sleep(1)
        nodes = wait_stable(driver, 3)
        sig = signature(nodes)
        if sig == prev:
            return None, nodes, k - 1
        prev = sig
        det = best_node(nodes, tt)
        if det is not None:
            return det, nodes, k
    return None, nodes, max_swipes


def vision_locate(driver, png, target):
    """Ask the vision model for the target's bounding box on the screenshot.
    Returns a synthetic node (so the normal tap path works) or None."""
    try:
        from pipeline.llm_client import chat_json
        prompt = (
            "This is a screenshot of an Android app. Find the button, link or text labelled "
            f'"{target}" and return its bounding box.\n'
            'Return ONLY JSON like {"found": true, "box": [ymin, xmin, ymax, xmax]} with each '
            "value an integer from 0 to 1000 (fraction of image height/width x 1000). "
            'If no element with that label is visible return {"found": false}. '
            "Do not guess a different element."
        )
        data = chat_json(prompt, png)
        if not data.get("found"):
            return None
        ymin, xmin, ymax, xmax = [float(v) for v in data["box"]]
        if not (0 <= ymin < ymax <= 1000 and 0 <= xmin < xmax <= 1000):
            return None
        size = driver.get_window_size()
        w, h = size["width"], size["height"]
        return {"cls": "vision.Located", "text": "", "desc": "", "rid": "",
                "clickable": True, "enabled": True,
                "x1": int(xmin * w / 1000), "y1": int(ymin * h / 1000),
                "x2": int(xmax * w / 1000), "y2": int(ymax * h / 1000)}
    except Exception:
        return None


def tap_variants(driver, node):
    """Taps that behave differently on tricky widgets (e.g. Flutter text buttons where
    mobile: clickGesture is ignored). Each returns a short description, or raises."""
    x, y = (node["x1"] + node["x2"]) // 2, (node["y1"] + node["y2"]) // 2

    def touch_hold():
        from selenium.webdriver.common.actions import interaction
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.pointer_input import PointerInput
        finger = PointerInput(interaction.POINTER_TOUCH, "finger")
        ab = ActionBuilder(driver, mouse=finger)
        ab.pointer_action.move_to_location(x, y)
        ab.pointer_action.pointer_down()
        ab.pointer_action.pause(0.15)
        ab.pointer_action.pointer_up()
        ab.perform()

    def adb_run(args):
        # Straight from the backend: no Appium 'adb_shell' feature flag needed.
        import subprocess
        caps = getattr(driver, "capabilities", {}) or {}
        udid = caps.get("deviceUDID") or caps.get("udid") or os.environ.get("ANDROID_SERIAL") or ""
        cmd = ["adb"] + (["-s", str(udid)] if udid else []) + ["shell", "input"] + args
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip() or f"adb exit {r.returncode}")

    def adb_tap():
        adb_run(["tap", str(x), str(y)])

    def adb_long_tap():
        adb_run(["swipe", str(x), str(y), str(x), str(y), "150"])

    return [("adb input tap", adb_tap), ("adb long tap (150ms)", adb_long_tap),
            ("touch action (150ms press)", touch_hold)]


def mark_tap(png_in, png_out, node, window):
    """Debug aid: copy the screenshot with the tap target drawn on it (step_N_tap.png)."""
    try:
        from PIL import Image, ImageDraw
        im = Image.open(png_in).convert("RGB")
        sx, sy = im.width / window["width"], im.height / window["height"]
        x1, y1, x2, y2 = node["x1"] * sx, node["y1"] * sy, node["x2"] * sx, node["y2"] * sy
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        d = ImageDraw.Draw(im)
        d.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=4)
        d.line([cx - 25, cy, cx + 25, cy], fill=(255, 0, 0), width=4)
        d.line([cx, cy - 25, cx, cy + 25], fill=(255, 0, 0), width=4)
        im.save(png_out)
    except Exception:
        pass


def edit_for(nodes, node):
    """Typing target: the EditText that belongs to `node` (the matched label may be a separate
    label view or a floating hint). Prefers an EditText that contains the node, else the nearest."""
    if node["cls"].endswith("EditText"):
        return node
    edits = [n for n in nodes if n["cls"].endswith("EditText")]
    if not edits:
        return None
    cx, cy = (node["x1"] + node["x2"]) // 2, (node["y1"] + node["y2"]) // 2
    for n in edits:
        if n["x1"] <= cx <= n["x2"] and n["y1"] <= cy <= n["y2"]:
            return n

    def dist(n):
        dx = max(0, n["x1"] - cx, cx - n["x2"])
        dy = max(0, n["y1"] - cy, cy - n["y2"])
        return dx + 2 * dy
    best = min(edits, key=dist)
    return best if dist(best) < 400 else None


def type_into(driver, node, value, trace_notes):
    """Type into the field that was just tapped.

    NEVER tap again here: once the keyboard is open, the field's old coordinates lie on the
    keyboard and a second tap types a letter ("ee12/30"). If the focused element cannot be
    read, address the field by its bounds, and as a last resort use adb. Existing text is
    cleared first so a re-run does not append to it."""
    text = str(value)
    errors = []
    time.sleep(0.6)  # let the keyboard animation finish
    try:
        el = driver.switch_to.active_element
        try:
            el.clear()
        except Exception:
            pass
        el.send_keys(text)
        return "focused element"
    except Exception as e:
        errors.append(f"active element: {str(e).splitlines()[0][:60]}")
    try:
        el = driver.find_element("xpath", f'//*[@bounds="{bounds_str(node)}"]')
        try:
            el.clear()
        except Exception:
            pass
        el.send_keys(text)
        return "element by bounds"
    except Exception as e:
        errors.append(f"by bounds: {str(e).splitlines()[0][:60]}")
    try:
        import subprocess
        caps = getattr(driver, "capabilities", {}) or {}
        udid = caps.get("deviceUDID") or caps.get("udid") or os.environ.get("ANDROID_SERIAL") or ""
        cmd = ["adb"] + (["-s", str(udid)] if udid else []) + ["shell", "input", "text",
                                                                 text.replace(" ", "%s")]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return "adb input text"
        errors.append(f"adb: {(r.stderr or r.stdout).strip()[:60]}")
    except Exception as e:
        errors.append(f"adb: {str(e)[:60]}")
    raise RuntimeError("could not type into the field (" + "; ".join(errors) + ")")


def recognize(png, nodes):
    """Phase 4 + 6 + 3: CLIP embedding, ChromaDB lookup, vision model label on a miss."""
    try:
        from pipeline.phase4_clip_encoder import get_image_vector
        from pipeline.phase6_chromadb_rag import find_similar_screen, store_screen
        vec = get_image_vector(png)
        hit = find_similar_screen(vec)
        if hit:
            return hit["label"], "memory hit", hit["similarity"]
        try:
            from pipeline.llm_client import label_screen
            label, source = label_screen(png), "new screen (vision model)"
        except Exception:
            texts = [t for t in dict.fromkeys(n["text"] or n["desc"] for n in nodes) if t][:15]
            label = "Screen Name: Unlabeled\nElements:\n" + "\n".join(f"- {t}" for t in texts)
            source = "new screen (UI tree label, vision model unavailable)"
        store_screen(vec, label, png)
        return label, source, None
    except Exception as e:
        return "", f"recognition failed: {e}", None


def ask_llm(action, target, value, screen_name, items, history, image_path=None):
    from pipeline.llm_client import chat_json
    lines = [
        f"{i}: {label} [{kind}{', clickable' if n['clickable'] else ''}]"
        for i, (n, label, kind) in enumerate(items)
    ]
    value_part = f' with value "{value}"' if value else ""
    done = "; ".join(history[-6:]) if history else "none"
    prompt = (
        "You are generating one Appium test step for an Android app.\n\n"
        f'Test plan item: {action} "{target}"{value_part}\n'
        f"Current screen: {screen_name}\n"
        "Elements visible on this screen (index: label [type]):\n"
        + "\n".join(lines)
        + f"\n\nSteps already executed: {done}\n\n"
        "Choose the single element that the test plan item refers to.\n"
        'Return ONLY JSON like {"element": 3} where 3 is the index from the list above.'
    )
    data = chat_json(prompt, image_path)
    idx = data.get("element")
    if isinstance(idx, list):
        idx = idx[0]
    return int(idx)


def xpath_for(n):
    for attr, val in (("text", n["text"]), ("content-desc", n["desc"])):
        if val:
            if '"' not in val:
                return f'//*[@{attr}="{val}"]', attr, val
            if "'" not in val:
                return f"//*[@{attr}='{val}']", attr, val
    return None, None, None


def bounds_str(n):
    return f"[{n['x1']},{n['y1']}][{n['x2']},{n['y2']}]"


def run_agent_steps(driver, actions, run_dir, trace):
    steps, screens, history = [], [], []
    trace.append("# Generated by VeroFlow agent mode: NLP actions + screen recognition + LLM element resolution")
    trace.append("from appium.webdriver.common.appiumby import AppiumBy")
    trace.append("")
    trace.append("def run(driver):")
    try:
        time.sleep(2)
        for i, a in enumerate(actions, 1):
            action = str(a.get("action", "tap")).lower().strip()
            target = str(a.get("target", "")).strip()
            value = a.get("value") or a.get("text") or a.get("input") or ""
            step = {"n": i, "action": action, "target": target, "matched": "", "status": "failed",
                    "screenshot": f"step_{i}.png", "error": "", "screen": "", "screen_source": "",
                    "similarity": None, "decided_by": "", "llm_seconds": None, "executed_via": ""}
            steps.append(step)
            scrolled = 0

            if action == "back":
                driver.back()
                trace.append(f"    driver.back()  # step {i}")
                step["status"] = "passed"
            elif action in ("wait", "sleep"):
                time.sleep(2)
                trace.append(f"    # step {i}: wait")
                step["status"] = "passed"
            elif action in ("scroll", "swipe"):
                # "scroll down" moves the content up, so the finger swipes up (and vice versa)
                want_up = "up" in target.lower()
                hide_keyboard(driver)
                size = driver.get_window_size()
                w, h = size["width"], size["height"]
                finger = "down" if want_up else "up"
                params = {"left": w // 4, "top": h // 4, "width": w // 2, "height": h // 2,
                          "direction": finger, "percent": 0.75}
                driver.execute_script("mobile: swipeGesture", params)
                trace.append(f"    driver.execute_script('mobile: swipeGesture', {params!r})  # step {i}: scroll")
                step["executed_via"] = "swipe gesture"
                step["status"] = "passed"
            else:
                tt = target_tokens(target)
                if action in TYPE_WORDS:
                    hide_keyboard(driver)  # a keyboard left open by the previous field hides the next one
                nodes = wait_stable(driver)
                det = best_node(nodes, tt)
                for _ in range(2):
                    if det is not None:
                        break
                    time.sleep(2)
                    nodes = wait_stable(driver)
                    det = best_node(nodes, tt)

                if not nodes:
                    # The UI tree is empty: the active window is not the app but a system overlay
                    # (e.g. the Android "Try out your stylus" dialog). Dismiss it with Back and re-read.
                    try:
                        driver.back()
                        trace.append(f"    driver.back()  # step {i}: dismissed a system overlay (empty UI tree)")
                        time.sleep(1.5)
                    except Exception:
                        pass
                    nodes = wait_stable(driver)
                    det = best_node(nodes, tt)

                before_png = f"{run_dir}/step_{i}_before.png"
                driver.get_screenshot_as_file(before_png)
                label, source, sim = recognize(before_png, nodes)
                name = screen_name_of(label)
                step.update({"screen": name, "screen_source": source, "similarity": sim})
                screens.append({"step": i, "screenshot": f"step_{i}_before.png", "screen_name": name,
                                "label": label, "source": source, "similarity": sim})

                items = describe_elements(nodes)
                chosen, decided, llm_s = None, "llm", None
                # Exactly one element carries the target's exact label: nothing for the LLM to
                # disambiguate, so skip the call (saves API quota and ~5-10 s per step).
                exact = [n for n in nodes if node_score(tt, n) >= 100]
                if len(exact) == 1 and action not in TYPE_WORDS:
                    chosen, decided, llm_s = exact[0], "exact label match (LLM skipped)", 0.0
                else:
                    t0 = time.time()
                    try:
                        idx = ask_llm(action, target, value, name, items, history, before_png)
                        if 0 <= idx < len(items):
                            chosen = items[idx][0]
                    except Exception as e:
                        decided = f"fallback (LLM error: {str(e)[:80]})"
                    llm_s = round(time.time() - t0, 1)
                step["llm_seconds"] = llm_s

                det_score = node_score(tt, det)
                chosen_score = node_score(tt, chosen)
                if chosen is None:
                    chosen = det
                    if not decided.startswith("fallback"):
                        decided = "fallback (LLM returned no valid element)"
                elif det is not None and det_score >= 60 and chosen_score < 20:
                    chosen, decided = det, "validator override (LLM choice contradicted an exact label match)"
                elif action in VERIFY_WORDS and chosen_score < 20:
                    chosen, decided = (det, "validator override (verify needs matching text)") if det else (None, decided)
                elif (action not in VERIFY_WORDS and det is None and chosen_score < 20
                      and node_label(chosen) and not chosen["cls"].endswith("EditText")
                      and not any(w in target.lower() for w in ICONISH)):
                    # The LLM picked a labelled element ("Next") for a text target ("Skip") that
                    # is not on screen. Tapping it anyway made wrong steps 'pass'; look further
                    # (scroll) and fail honestly instead.
                    chosen, decided = None, "rejected (LLM pick does not match target text)"

                if chosen is None and action not in VERIFY_WORDS and action not in TYPE_WORDS:
                    # Not in the accessibility tree (e.g. a button drawn under the status bar):
                    # ask the vision model where it is on the screenshot and tap that point.
                    vnode = vision_locate(driver, before_png, target)
                    if vnode is not None:
                        chosen, decided = vnode, "vision fallback (not in UI tree, located on screenshot)"
                        trace.append(f"    # step {i}: '{target}' not in UI tree, located by vision model")

                if chosen is None:
                    found, nodes, scrolled = scroll_find(driver, tt, nodes)
                    items = describe_elements(nodes)
                    if found is not None:
                        chosen, decided = found, f"auto-scroll ({scrolled} swipes to find it)"
                        trace.append(f"    # step {i}: target not visible, scrolled {scrolled}x to find it")

                if chosen is None:
                    labels = [lbl for _, lbl, _ in items][:30]
                    msg = (f"Step {i} ({action} '{target}') failed: no element on screen could be "
                           f"resolved. Visible on screen: {labels}")
                    step["error"] = msg
                    try:
                        driver.get_screenshot_as_file(f"{run_dir}/step_{i}.png")
                    except Exception:
                        pass
                    raise RuntimeError(msg)

                step["decided_by"] = decided
                step["matched"] = node_label(chosen) or target
                ctrl = (toggle_in_row(nodes, chosen)
                        if (action not in VERIFY_WORDS and action not in TYPE_WORDS
                            and not chosen["cls"].startswith("vision.")) else None)
                node = ctrl if (ctrl is not None) else chosen
                if action in TYPE_WORDS:
                    node = edit_for(nodes, chosen) or chosen
                # Record what was really tapped (may differ from the matched label for chips/toggles)
                step["tapped"] = node_label(node) or f"{node['cls'].split('.')[-1]} {bounds_str(node)}"
                xp, attr, val = xpath_for(node)
                head = f"    # step {i} | screen: {name} ({source}) | resolved by {decided}"
                trace.append(head)

                if action in VERIFY_WORDS:
                    trace.append(f"    assert driver.find_element(AppiumBy.XPATH, '{xp}') is not None" if xp
                                 else f"    # verify: element '{node_label(chosen)}' present")
                    step["executed_via"] = "tree check"
                else:
                    exact = f'//*[@{attr}="{val}" and @bounds="{bounds_str(node)}"]' if (xp and '"' not in val) else None
                    done = False
                    if exact:
                        try:
                            el = driver.find_element("xpath", exact)
                            el.click()
                            step["executed_via"] = "locator"
                            done = True
                        except Exception:
                            done = False
                    if not done:
                        tap_node(driver, node)
                        step["executed_via"] = "coordinates"
                        mark_tap(before_png, f"{run_dir}/step_{i}_tap.png", node, driver.get_window_size())
                        if chosen["cls"].startswith("vision."):
                            time.sleep(1.5)
                            changed = signature(wait_stable(driver, 3)) != signature(nodes)
                            tried = ["clickGesture"]
                            if not changed:
                                for name, fn in tap_variants(driver, node):
                                    try:
                                        fn()
                                    except Exception as e:
                                        tried.append(f"{name} (unavailable: {str(e)[:60]})")
                                        continue
                                    tried.append(name)
                                    time.sleep(1.5)
                                    if signature(wait_stable(driver, 3)) != signature(nodes):
                                        changed = True
                                        step["executed_via"] = f"coordinates ({name})"
                                        break
                            if not changed:
                                step["error"] = (f"Step {i} ({action} '{target}') failed: tapped at "
                                                 f"{bounds_str(node)} (located by vision) using {', '.join(tried)} "
                                                 f"but the screen did not change. See step_{i}_tap.png for where "
                                                 f"the tap landed.")
                                try:
                                    driver.get_screenshot_as_file(f"{run_dir}/step_{i}.png")
                                except Exception:
                                    pass
                                raise RuntimeError(step["error"])
                    if action in TYPE_WORDS:
                        time.sleep(0.5)
                        how = type_into(driver, node, value, trace)
                        step["executed_via"] = f"{step['executed_via']} + typed via {how}"
                        hide_keyboard(driver)  # so the next field is not hidden under the keyboard
                        trace.append(f"    driver.find_element(AppiumBy.XPATH, '{xp}').send_keys({str(value)!r})" if xp
                                     else f"    # type {str(value)!r} into '{node_label(chosen)}'")
                    else:
                        trace.append(f"    driver.find_element(AppiumBy.XPATH, '{xp}').click()" if xp
                                     else f"    driver.execute_script('mobile: clickGesture', "
                                          f"{{'x': {(node['x1'] + node['x2']) // 2}, 'y': {(node['y1'] + node['y2']) // 2}}})")
                history.append(f"{action} {node_label(chosen) or target}")
                step["status"] = "passed"

            time.sleep(2)
            try:
                driver.get_screenshot_as_file(f"{run_dir}/step_{i}.png")
            except Exception:
                pass
            # A verify that had to scroll must not leave the list scrolled for the next step
            if action in VERIFY_WORDS and step.get("status") == "passed" and scrolled:
                for _ in range(scrolled):
                    try:
                        swipe(driver, "down")
                    except Exception:
                        break
                time.sleep(1)
    finally:
        for fname, data in (("steps.json", steps), ("screens.json", screens)):
            try:
                with open(f"{run_dir}/{fname}", "w") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
    return trace