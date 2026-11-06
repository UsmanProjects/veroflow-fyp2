import re
import json
import time
import xml.etree.ElementTree as ET

TAP_WORDS = {"tap", "click", "press", "select", "choose", "open", "toggle", "tick"}
TYPE_WORDS = {"type", "enter", "input", "fill", "write"}
VERIFY_WORDS = {"verify", "assert", "confirm", "check", "expect", "see"}
DROP_WORDS = {"button", "btn", "field", "icon", "link", "tab", "checkbox", "radio", "option", "the", "arrow"}
TOGGLE_CLASSES = ("CheckBox", "RadioButton", "Switch", "ToggleButton", "CompoundButton")


def tokens(s):
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def target_tokens(target):
    return [t for t in tokens(target) if t not in DROP_WORDS]


def rid_label(rid):
    last = (rid or "").split("/")[-1]
    last = re.sub(r"([a-z])([A-Z])", r"\1 \2", last)
    return last.replace("_", " ")


def visible_nodes(driver):
    root = ET.fromstring(driver.page_source)
    nodes = []
    for el in root.iter():
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.attrib.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        nodes.append({
            "cls": el.attrib.get("class", el.tag),
            "text": el.attrib.get("text", ""),
            "desc": el.attrib.get("content-desc", ""),
            "rid": rid_label(el.attrib.get("resource-id", "")),
            "clickable": el.attrib.get("clickable") == "true",
            "enabled": el.attrib.get("enabled") != "false",
            "checked": el.attrib.get("checked") == "true",
            "selected": el.attrib.get("selected") == "true",
            "focused": el.attrib.get("focused") == "true",
            "password": el.attrib.get("password") == "true",
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        })
    return nodes


def label_score(tt, label):
    lt = tokens(label)
    if not lt or not tt:
        return 0
    if lt == tt:
        return 100
    ts, ls = set(tt), set(lt)
    if ts <= ls:
        return 60 + 30 * len(ts) / len(ls)
    overlap = len(ts & ls) / len(ts)
    if len(ts) >= 3 and overlap >= 0.67:
        return 30 * overlap
    return 0


def best_node(nodes, tt):
    best, best_key = None, None
    for n in nodes:
        s = max(label_score(tt, n["text"]), label_score(tt, n["desc"]), label_score(tt, n["rid"]))
        if s < 20:
            continue
        if n["clickable"]:
            s += 5
        if not n["enabled"]:
            s -= 20
        key = (-s, n["y1"], n["x1"])
        if best_key is None or key < best_key:
            best, best_key = n, key
    return best


def is_toggle(n):
    return n["cls"].endswith(TOGGLE_CLASSES)


def toggle_in_row(nodes, node):
    """If the matched label belongs to a checkbox/radio/switch/chip, return that control.

    Controls in the same row are ranked by horizontal distance to the label
    (0 = the label sits inside the control, e.g. a filter chip). The old version
    compared vertical distance only, so in a row of chips every chip tied and the
    first chip was always returned. If several controls are equally close, return
    None so the caller taps the label itself instead of guessing."""
    if is_toggle(node):
        return node
    cy = (node["y1"] + node["y2"]) // 2
    # Wide vertical tolerance: in checkbox rows the label text is often top-aligned,
    # so its centre can sit ~70px above the checkbox centre. Rows are >150px apart.
    tol = max(90, node["y2"] - node["y1"])
    cands = []
    for n in nodes:
        if n is node or not is_toggle(n):
            continue
        ncy = (n["y1"] + n["y2"]) // 2
        vdist = abs(ncy - cy)
        if vdist > tol:
            continue
        gap = max(0, node["x1"] - n["x2"], n["x1"] - node["x2"])
        area = (n["x2"] - n["x1"]) * (n["y2"] - n["y1"])
        cands.append((gap, vdist, area, n))
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    if len(cands) > 1 and cands[0][:2] == cands[1][:2] and cands[0][0] != 0:
        return None
    return cands[0][3]


def screen_labels(nodes, limit=40):
    seen = []
    for n in nodes:
        for v in (n["text"], n["desc"]):
            if v and v not in seen:
                seen.append(v)
    return seen[:limit]


def find_node(driver, target, timeout=10):
    tt = target_tokens(target)
    deadline = time.time() + timeout
    labels = []
    while True:
        try:
            nodes = visible_nodes(driver)
            labels = screen_labels(nodes)
            node = best_node(nodes, tt)
            if node:
                return node, nodes, labels
        except ET.ParseError:
            pass
        if time.time() > deadline:
            return None, [], labels
        time.sleep(1)


def tap_node(driver, node):
    x = (node["x1"] + node["x2"]) // 2
    y = (node["y1"] + node["y2"]) // 2
    driver.execute_script("mobile: clickGesture", {"x": x, "y": y})
    return x, y


def run_steps(driver, actions, run_dir, trace):
    steps = []
    try:
        time.sleep(2)
        for i, a in enumerate(actions, 1):
            action = str(a.get("action", "tap")).lower().strip()
            target = str(a.get("target", "")).strip()
            value = a.get("value") or a.get("text") or a.get("input") or ""
            step = {"n": i, "action": action, "target": target, "matched": "",
                    "status": "failed", "screenshot": f"step_{i}.png", "error": ""}
            steps.append(step)

            if action == "back":
                driver.back()
                trace.append(f"# step {i}: back")
                step["status"] = "passed"
            elif action in ("wait", "sleep"):
                time.sleep(2)
                trace.append(f"# step {i}: wait")
                step["status"] = "passed"
            else:
                node, nodes, labels = find_node(driver, target, timeout=10)
                if node is None:
                    msg = (f"Step {i} ({action} '{target}') failed: nothing on screen matches. "
                           f"Visible on screen: {labels}")
                    step["error"] = msg
                    try:
                        driver.get_screenshot_as_file(f"{run_dir}/step_{i}.png")
                    except Exception:
                        pass
                    raise RuntimeError(msg)
                shown = node["text"] or node["desc"] or node["rid"]
                step["matched"] = shown
                if action in VERIFY_WORDS:
                    trace.append(f"# step {i}: verify '{target}' -> found {shown!r}")
                elif action in TYPE_WORDS:
                    x, y = tap_node(driver, node)
                    time.sleep(0.5)
                    driver.switch_to.active_element.send_keys(str(value))
                    trace.append(f"# step {i}: type {value!r} into '{target}' -> {shown!r} at ({x},{y})")
                else:
                    ctrl = toggle_in_row(nodes, node)
                    if ctrl is not None and ctrl is not node:
                        x, y = tap_node(driver, ctrl)
                        trace.append(f"# step {i}: tap '{target}' -> {shown!r} (control {ctrl['cls'].split('.')[-1]}) at ({x},{y})")
                    else:
                        x, y = tap_node(driver, node)
                        trace.append(f"# step {i}: tap '{target}' -> {shown!r} at ({x},{y})")
                step["status"] = "passed"

            time.sleep(2)
            try:
                driver.get_screenshot_as_file(f"{run_dir}/step_{i}.png")
            except Exception:
                pass
    finally:
        try:
            with open(f"{run_dir}/steps.json", "w") as f:
                json.dump(steps, f, indent=2)
        except Exception:
            pass
    return trace