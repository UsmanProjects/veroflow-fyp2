from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
import os
import sys
import json
import time
from datetime import datetime


def save_run_artifacts(run_dir, instruction, result):
    """Saves every phase's output as a separate file inside one run folder."""

    with open(f"{run_dir}/instruction.txt", "w") as f:
        f.write(instruction)

    with open(f"{run_dir}/parsed_actions.json", "w") as f:
        json.dump(result.get("parsed_actions", []), f, indent=2)

    with open(f"{run_dir}/screen_label.txt", "w") as f:
        f.write(result.get("screen_label", ""))

    with open(f"{run_dir}/rag_match.json", "w") as f:
        json.dump({"screen_source": result.get("screen_source", "")}, f, indent=2)

    with open(f"{run_dir}/generated_code.py", "w") as f:
        f.write(result.get("generated_code", ""))

    with open(f"{run_dir}/result.json", "w") as f:
        json.dump(result, f, indent=2)


def run_full_test(apk_path: str, instruction: str):
    from pipeline.step_runner import run_steps
    from pipeline.agent_runner import run_agent_steps
    from pipeline.quick_parser import quick_parse
    from pipeline.phase1_nlp_parser import parse_instruction
    from pipeline.llm_client import label_screen
    from pipeline.goal_runner import goal_actions, looks_like_test_case, run_goal_steps
    from pipeline.phase4_clip_encoder import get_image_vector
    from pipeline.phase6_chromadb_rag import find_similar_screen, store_screen

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = f"storage/runs/run_{timestamp}"
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs("storage/chroma_data", exist_ok=True)

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.device_name = "emulator-5554"
    options.app = apk_path
    options.automation_name = "UiAutomator2"
    options.new_command_timeout = 900
    options.app_wait_activity = "*"
    options.app_wait_duration = 60000
    options.adb_exec_timeout = 60000
    options.uiautomator2_server_launch_timeout = 60000
    options.auto_grant_permissions = True
    # VEROFLOW_NO_RESET=1 keeps the app's data between runs (e.g. stays logged in). Default: clean start.
    options.no_reset = os.environ.get("VEROFLOW_NO_RESET") == "1"

    driver = webdriver.Remote("http://127.0.0.1:4723", options=options)
    # VEROFLOW_NO_IDLE_WAIT=1: do not wait for the app to be "idle" before reading the screen. Needed for
    # apps with endless animations, where UiAutomator2 otherwise times out or crashes on page_source.
    if os.environ.get("VEROFLOW_NO_IDLE_WAIT") == "1":
        try:
            driver.update_settings({"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0})
        except Exception:
            pass

    result = {
        "instruction": instruction,
        "parsed_actions": [],
        "screen_label": "",
        "screen_source": "",
        "status": "unknown",
        "generated_code": "",
        "error": None,
        "before_screenshot": "",
        "after_screenshot": ""
    }

    try:
        # GOAL mode: a test case (goal + expected result) instead of click-by-click steps.
        goal_mode = os.environ.get("VEROFLOW_MODE") == "goal" or looks_like_test_case(instruction)
        if goal_mode:
            parsed_actions = goal_actions(instruction)
        else:
            parsed_actions = quick_parse(instruction) or parse_instruction(instruction)
        result["parsed_actions"] = parsed_actions

        if not parsed_actions:
            raise ValueError("NLP parser could not extract any actions from the instruction")

        time.sleep(3)
        before_path = f"{run_dir}/before.png"
        driver.get_screenshot_as_file(before_path)
        result["before_screenshot"] = before_path

        trace = []
        try:
            if goal_mode:
                run_goal_steps(driver, instruction, run_dir, trace)
            else:
                (run_agent_steps if os.environ.get("VEROFLOW_MODE") == "agent" else run_steps)(driver, parsed_actions, run_dir, trace)
        finally:
            result["generated_code"] = "\n".join(trace)
            try:   # computed from this run's own files (steps.json, goal_result.json), saved as metrics.json
                from pipeline.goal_runner import compute_metrics
                result["metrics"] = compute_metrics(run_dir, "goal" if goal_mode else "script",
                                                    "PASSED" if sys.exc_info()[0] is None else "FAILED")
                with open(f"{run_dir}/metrics.json", "w") as f:
                    json.dump(result["metrics"], f, indent=2)
            except Exception:
                pass
            if goal_mode:
                # Goal mode has no fixed list of steps: show what the agent really did, so the dashboard's
                # "x of y steps passed" divides by the executed steps (not by the test-case rows).
                try:
                    with open(f"{run_dir}/steps.json") as f:
                        executed = json.load(f)
                    if executed:
                        result["parsed_actions"] = [{"action": st["action"], "target": st["target"]} for st in executed]
                except Exception:
                    pass

        after_path = f"{run_dir}/after.png"
        driver.get_screenshot_as_file(after_path)
        result["after_screenshot"] = after_path

        result["status"] = "passed"

    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        try:
            error_path = f"{run_dir}/error.png"
            driver.get_screenshot_as_file(error_path)
            result["after_screenshot"] = error_path
        except:
            pass

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # Phase 3/4/6 (heavy AI work) runs AFTER the device session is closed,
    # so the emulator and Appium stay responsive while the steps execute.
    if result["before_screenshot"]:
        try:
            vector = get_image_vector(result["before_screenshot"])
            existing_screen = find_similar_screen(vector)

            if existing_screen:
                screen_label = existing_screen["label"]
                result["screen_source"] = f"Reused from memory (similarity: {existing_screen['similarity']})"
            else:
                screen_label = label_screen(result["before_screenshot"])
                store_screen(vector, screen_label, result["before_screenshot"])
                result["screen_source"] = "New screen — labeled and stored"

            result["screen_label"] = screen_label
        except Exception as e:
            result["screen_source"] = f"Screen recognition skipped: {e}"

    save_run_artifacts(run_dir, instruction, result)
    result["run_folder"] = run_dir

    return result


if __name__ == "__main__":
    result = run_full_test(
        apk_path=r"/path/to/your/app.apk",
        instruction="tap the login button"
    )
    print(result)