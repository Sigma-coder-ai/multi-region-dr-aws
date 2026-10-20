"""Wait until the live controller's scoring window is all healthy (used before REAL-AWS drills)."""
import json, os, sys, time
st = json.load(open(".aws-deploy.json")); os.environ.setdefault("AWS_PROFILE", "mahesh-project"); sys.path.insert(0, "src")
from drsystem.aws.s3store import S3ObjectStore
s = S3ObjectStore(st["buckets"][st["dr"]], st["dr"])
deadline = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 900)
while time.time() < deadline:
    d = s.get("local/controller-state.json").json()
    hist = d["history"].get(st["home"], [])
    if len(hist) >= 6 and all(p["ok"] for p in hist[-6:]) and d["last_score"] >= 90:
        print(time.strftime("%H:%M:%S"), "controller baseline healthy, score", d["last_score"], flush=True); sys.exit(0)
    time.sleep(20)
print("timed out waiting for a healthy baseline", flush=True); sys.exit(1)
