from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os
import shutil

from pipeline.phase1_nlp_parser import parse_instruction
from pipeline.test_executor import run_full_test

app = FastAPI()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
os.makedirs("storage/apks", exist_ok=True)
os.makedirs("storage/screenshots", exist_ok=True)
os.makedirs("storage/chroma_data", exist_ok=True)
os.makedirs("storage/runs", exist_ok=True)  # For storing screenshots and logs of test runs


@app.get("/")
def home():
    return {"message": "VeroFlow backend is working!"}


@app.post("/upload-apk")
async def upload_apk(file: UploadFile = File(...)):
    save_path = os.path.abspath(f"storage/apks/{file.filename}")
    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return {"message": "APK uploaded successfully", "path": save_path}


@app.post("/submit-test")
async def submit_test(instruction: str = Form(...)):
    actions = parse_instruction(instruction)
    return {"instruction": instruction, "parsed_actions": actions}


@app.post("/run-test")
async def run_test(apk_path: str = Form(...), instruction: str = Form(...)):
    try:
        result = run_full_test(apk_path=apk_path, instruction=instruction)
        return result 
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Serves screenshot images so the Next.js dashboard can display them
app.mount("/runs", StaticFiles(directory="storage/runs"), name="runs")

import json


@app.get("/runs-list")
def runs_list():
    """Summary of the most recent test runs, newest first (used by the dashboard history tab)."""
    base = "storage/runs"
    items = []
    for name in sorted(os.listdir(base), reverse=True)[:40]:
        folder = os.path.join(base, name)
        result_path = os.path.join(folder, "result.json")
        if not (name.startswith("run_") and os.path.isfile(result_path)):
            continue
        try:
            with open(result_path) as f:
                r = json.load(f)
        except Exception:
            continue
        passed = None
        steps_path = os.path.join(folder, "steps.json")
        if os.path.isfile(steps_path):
            try:
                with open(steps_path) as f:
                    passed = sum(1 for s in json.load(f) if s.get("status") == "passed")
            except Exception:
                passed = None
        items.append({
            "run": name,
            "status": r.get("status"),
            "instruction": (r.get("instruction") or "")[:160],
            "total_steps": len(r.get("parsed_actions") or []),
            "passed_steps": passed,
        })
    return items
