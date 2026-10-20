# How to Run and Demo the DR System

## Easiest way: double-click
| File (in `D:\AWS Project`) | What it does |
|---|---|
| **`Start-Demo.bat`** | Sets everything up on first run, starts the simulated system and opens the dashboard in your browser. Close the "DR Simulation" window to stop it. |
| **`DR-Menu.bat`** | A numbered menu: demo, tests, simulated drills, optimizer, AWS sign-in, AWS status, zero-spend check, real drills, teardown (asks you to type DELETE), open results or docs. |

Everything below is what those files run, if you prefer typing commands.

All commands run in **PowerShell** in `D:\AWS Project`. Two modes:

| Mode | What it is | Cost | Label on results |
|---|---|---|---|
| **Local simulation** | Two "regions" on your laptop + dashboard | $0, offline | `SIMULATED` |
| **Real AWS** | The deployed system in Mumbai + Singapore | $0 out-of-pocket (Free plan credits) | `REAL-AWS` |

---

## 0. One-time setup (each team member's laptop)
```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest            # expect: 72 passed
```

---

## 1. Local demo with the live dashboard (best for a presentation, 5 minutes)
```powershell
.venv\Scripts\python -m drsystem sim
```
Open **http://127.0.0.1:8100/** in a browser. Stop with `Ctrl+C`.

| Card | Meaning |
|---|---|
| **Serving region** | Which region answered your last read (green = Mumbai, orange = Singapore) |
| **Write authority** | Which region may accept writes, plus the epoch number (fencing token) |
| **Health score** | 0–100 score of the active region and the controller's decision |
| **Regions table** | Injected faults, replication backlog, each region's view of the control object |
| **Timeline** | Every detection, decision, promotion and verification event |

### Demo script
1. **Normal state:** both regions green, Mumbai serves and holds write authority (epoch 1).
   Click **Write a note**. It shows "stored in Mumbai".
2. **Disaster:** under *Mumbai*, click **Outage**. Within about 5 seconds:
   - the health score drops; the timeline shows `first_failed_probe`, then `decision` (FAILOVER);
   - **Serving region** turns orange: Singapore;
   - **Write authority** becomes Singapore, epoch 2, "demoted: ap-south-1";
   - the timeline shows the workflow: reassess → drain → promote → notify → verify.
3. **The system still works:** click **Write a note** again. It shows "stored in Singapore".
4. **Recovery:** under *Mumbai*, click **Clear**. Mumbai stays out of service (demoted): **no automatic flap-back**.
5. **Controlled failback:** wait about 10 s (cool-down), click **Approve controlled failback**, enter your name.
   The timeline shows: freeze writes → drain → verify data → promote home → verify traffic. Mumbai is back (epoch 4).
6. **Intelligence (optional):** click Mumbai **70% errors** (gray failure, also fails over after the hysteresis) or
   **Health fail** then **Clear** quickly (a blip, so no failover). For a dual failure: Mumbai **Storage down** +
   Singapore **Health fail**. It refuses to fail over to a broken region and sends **one** alert.

### Automated simulated drills (numbers for the report's "simulation" section)
```powershell
.venv\Scripts\python -m drsystem scenarios
.venv\Scripts\python -m drsystem drill all --repeat 3 --failback
.venv\Scripts\python -m drsystem drill hard-outage --repeat 5 --failback
```
Reports go to `evidence\simulated\<suite>\SUMMARY.md` (plus one `.md`/`.json` per drill).

### Recovery-time optimizer (model, not a measurement)
```powershell
.venv\Scripts\python -m drsystem optimize --target-rto 300 --target-rpo 60 --replication-lag 26
```
Report: `evidence\model\optimizer.md`. It shows the cheapest settings that meet the targets.

---

## 2. Real AWS (the deployed system)
### Sign in (when the session has expired; sessions last a few hours)
```powershell
aws login --profile mahesh-project
```
### Health of both regions
```powershell
.venv\Scripts\python scripts\aws_deploy.py status --profile mahesh-project
```
Expected: both `ok=True`, Mumbai `active=True`, the same epoch in both control views.

### Zero-spend check (read-only, before any demo)
```powershell
.venv\Scripts\python -m drsystem preflight --profile mahesh-project
```
Must end with `RESULT: OK` (plan FREE/ACTIVE, credits, days left).

### Run a real disaster drill (about 15 minutes each)
```powershell
.venv\Scripts\python scripts\aws_drill.py hard-outage --profile mahesh-project --failback
.venv\Scripts\python scripts\aws_drill.py gray-errors --profile mahesh-project --failback
.venv\Scripts\python scripts\aws_drill.py health-blip --profile mahesh-project
```
- Before a drill, run `scripts\wait_healthy.py` so the controller starts from a clean baseline.
- Between drills, wait at least 5 minutes after a failback (cool-down).
- Results go to `evidence\real-aws\drills\<date>\` (labelled REAL-AWS).

### What to show in the AWS console during a real drill (sign in as Mahesh)
| Console page | Region | What you see |
|---|---|---|
| **Lambda** → `drsys-app` | Mumbai and Singapore | The same app deployed in both regions |
| **Lambda** → `drsys-controller` → Monitor → **View CloudWatch logs** | Singapore | Each minute's tick; during a drill, the failover run |
| **EventBridge** → Schedules → `drsys-controller-tick` | Singapore | `rate(1 minute)` |
| **S3** → `drsys-…-ap-south-1` → **Management → Replication rules** | Mumbai | Replication to Singapore (and the reverse rule in the other bucket) |
| **S3** → bucket → `control/active-region.json` → Open | either | `active_region`, `epoch`, `demoted_region`: the failover switch |
| **S3** → bucket → `local/events/<date>/` | Singapore | The durable timeline (detection, decision, promotion events) |
| **Systems Manager → Parameter Store** → `/drsys/faults/ap-south-1` | Mumbai | The fault flag the drill sets (e.g. `{"outage": true}`) |
| **Billing → Budgets** | global | `$0.00` actual spend |

### Tear everything down (when the project is finished)
```powershell
.venv\Scripts\python scripts\aws_deploy.py teardown --profile mahesh-project
```
It deletes every resource and verifies by tag search that nothing is left. To deploy again:
```powershell
.venv\Scripts\python scripts\aws_deploy.py deploy --profile mahesh-project --email <your-email>
```

---

## 3. Results to cite in the report
- **REAL-AWS:** `docs\07-real-aws-results.md`
  - hard outage: write RTO 176 s, RPO 2.6 s;
  - failbacks: 0 writes lost;
  - health blip: no false failover;
  - the model predicted 167 s against the 176 s measured.
- **SIMULATED:** `docs\03-local-implementation.md` §5. Logic validation only; never mix it with REAL-AWS numbers.
