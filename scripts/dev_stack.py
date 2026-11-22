"""Start/stop the hybrid deployment without touching unrelated processes."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]


def birth(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (FileNotFoundError, ProcessLookupError): return None


def reachable(url):
    try: return httpx.get(url, timeout=2, trust_env=False).status_code == 200
    except httpx.HTTPError: return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--api-port", type=int, default=8001)
    parser.add_argument("--ui-port", type=int, default=8501)
    args = parser.parse_args()
    if args.api_port == args.ui_port or not all(0 < p < 65536 for p in (args.api_port, args.ui_port)):
        parser.error("Use two distinct valid ports")
    directory = ROOT/"data/cache/dev"/f"{args.api_port}-{args.ui_port}"
    manifest = directory/"processes.json"
    records = json.loads(manifest.read_text()) if manifest.exists() else []
    if args.action == "status":
        print(json.dumps({"api": reachable(f"http://127.0.0.1:{args.api_port}/health"),
            "streamlit": reachable(f"http://127.0.0.1:{args.ui_port}/_stcore/health"),
            "processes": [{**row, "owned_process_alive": birth(row["pid"]) == row["birth"]} for row in records]}, indent=2))
        return
    if args.action == "stop":
        for row in reversed(records):
            if birth(row["pid"]) == row["birth"]:
                os.kill(row["pid"], signal.SIGTERM)
        deadline = time.monotonic()+20
        while time.monotonic() < deadline and any(birth(r["pid"]) == r["birth"] for r in records): time.sleep(.2)
        print("Stop requested only for recorded processes with matching creation identity.")
        return
    if any(birth(row["pid"]) == row["birth"] for row in records):
        raise SystemExit("This stack is already running; use status or stop first")
    records = []
    for port in (args.api_port, args.ui_port):
        with socket.socket() as listener:
            try: listener.bind(("127.0.0.1", port))
            except OSError: raise SystemExit(f"Port {port} is occupied; choose another port") from None
    directory.mkdir(parents=True, exist_ok=True)
    api_env = {**os.environ, "PYTHONUNBUFFERED": "1", "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4"}
    ui_env = {key: value for key, value in os.environ.items()
              if not key.startswith(("LLM_", "TAVILY_", "DATABASE_", "CREDENTIALS_"))}
    ui_env.update(API_BASE_URL=f"http://127.0.0.1:{args.api_port}", PYTHONUNBUFFERED="1")
    commands = [
        ("api", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.api_port)], api_env, f"http://127.0.0.1:{args.api_port}/health"),
        ("streamlit", [sys.executable, "-m", "streamlit", "run", "frontend/streamlit_app.py", "--server.address=127.0.0.1", f"--server.port={args.ui_port}"], ui_env, f"http://127.0.0.1:{args.ui_port}/_stcore/health"),
    ]
    try:
        for name, command, env, url in commands:
            with (directory/f"{name}.log").open("a") as log:
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            record = {"service": name, "pid": process.pid, "birth": birth(process.pid), "url": url}
            records.append(record)
            manifest.write_text(json.dumps(records, indent=2)+"\n")
            deadline = time.monotonic()+60
            while time.monotonic() < deadline and process.poll() is None:
                if reachable(url): break
                time.sleep(.3)
            else: raise RuntimeError(f"{name} failed health check; inspect {directory/name}.log")
        print(f"API http://127.0.0.1:{args.api_port} | Streamlit http://127.0.0.1:{args.ui_port}")
    except BaseException:
        for row in records:
            if birth(row["pid"]) == row["birth"]: os.kill(row["pid"], signal.SIGTERM)
        raise


if __name__ == "__main__": main()
