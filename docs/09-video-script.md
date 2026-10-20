# 5-Minute Project Video: Script and Screen Plan

**Speaking pace:** about 140 words a minute, so roughly 700 words in total.
**Legend:** **[SHOW]** = what is on screen; *italics* = the words you say.

## Before you record (10 minutes, once)
1. Double-click `DR-Menu.bat`. Press **5** (sign in as Mahesh), then **6** (status): both regions `ok=True`, Mumbai active.
2. Optional, for live proof: press **8** about 20 minutes before recording, so a fresh real drill result is ready.
3. Sign in to the AWS console in Chrome as **Mahesh**, then open these tabs in advance:
   - Lambda (Mumbai)
   - Lambda (Singapore)
   - S3
   - EventBridge Schedules (Singapore)
   - CloudWatch log group `/aws/lambda/drsys-controller` (Singapore)
   - Systems Manager → Parameter Store (Mumbai)
   - Billing → Budgets
4. **Hide your 12-digit account ID** (top-right menu) when recording, or blur it afterwards.
5. Close the demo afterwards: close the black "DR Simulation" window.

---

## Part 1: Introduction (0:00–0:30)
**[SHOW]** Title slide or `README.md`: "Intelligent Multi-Region Disaster Recovery System". Team names.

*Hello, I'm Mahesh, and with Aarya and Dhrubajyoti we built an Intelligent Multi-Region Disaster
Recovery System on AWS. If a whole cloud region fails, an application normally goes down with it.
Our system keeps a second copy running in another region, detects failures on its own, switches users
over automatically, and measures exactly how fast it recovered and how much data was lost. Those two
numbers are called RTO and RPO. And we did it for zero rupees, using only AWS free credits.*

## Part 2: Architecture (0:30–1:15)
**[SHOW]** `docs/01-architecture.md` diagram (scroll to "Big picture").

*Mumbai is our primary region and Singapore is the disaster recovery region. In each region the
application runs on AWS Lambda and stores its data in Amazon S3. S3 Cross-Region Replication copies
every write between the two regions automatically.*

*The intelligent part is a controller that runs in Singapore every minute. It probes both regions five
times, computes a health score from availability, latency and readiness, and only fails over when the
score stays bad. That way a short blip never causes a false alarm.*

*Failover is controlled by one small file in S3, the control object. It says which region is allowed
to accept writes, with an epoch number. This prevents both regions writing at the same time,
which is called split-brain.*

## Part 3: Live demo on the dashboard (1:15–2:45)
**[SHOW]** Double-click `Start-Demo.bat`. The dashboard opens.

*This is our DR Live Monitor, running as a local simulation of the two regions. Right now Mumbai serves
users and holds write authority, epoch 1, health score 100.*

**[SHOW]** Click **Write a note**. It says "stored in Mumbai".

*Let's create a disaster.*

**[SHOW]** Under Mumbai, click **Outage**. Point at the timeline as the events appear.

*The controller detects failed probes, decides to fail over, re-checks so it is sure, waits for
replication, and then promotes Singapore. In a few seconds, Singapore is serving and has write authority,
epoch 2, and Mumbai is marked demoted.*

**[SHOW]** Click **Write a note**. It says "stored in Singapore".

*Users can still write. Now Mumbai recovers.*

**[SHOW]** Click **Clear** for Mumbai.

*Notice it does not jump back automatically, because flip-flopping between regions is dangerous.
Failback needs a human approval.*

**[SHOW]** Wait about 10 seconds. Click **Approve controlled failback**, type your name. Point at the timeline.

*The system freezes writes for a moment, checks that both regions have identical data, and only then
moves back to Mumbai. Zero data is lost in a planned failback.*

## Part 4: The real system on AWS (2:45–4:15)
**[SHOW]** `DR-Menu.bat` → **6**: both regions healthy.

*The same code is deployed on real AWS in both regions. Here is the live status: both healthy,
Mumbai active.*

**[SHOW]** Lambda console, Mumbai, then Singapore: function `drsys-app`.

*Here is the application function in Mumbai and the identical one in Singapore.*

**[SHOW]** S3 → bucket `drsys-…-ap-south-1` → **Management** → **Replication rules**.

*These are our two S3 buckets, with replication rules in both directions.*

**[SHOW]** Open `control/active-region.json`.

*This is the control object: the single source of truth for which region is active.*

**[SHOW]** EventBridge → Schedules → `drsys-controller-tick` (`rate(1 minute)`), then the CloudWatch logs of
`drsys-controller`.

*EventBridge Scheduler runs the controller every minute, and these are its logs.*

**[SHOW]** Systems Manager → Parameter Store → `/drsys/faults/ap-south-1`.

*During a drill we inject a real fault through this parameter. For example, Mumbai stops responding,
and we also pause replication.*

**[SHOW]** Lambda → `drsys-app` → **Configuration** → **Function URL**: Auth type **AWS_IAM**.

*Security: the endpoints are not public. Every request must be signed with AWS credentials, so nobody
on the internet can misuse them.*

**[SHOW]** Billing → Budgets: **$0.00**. Mention the Free plan.

*And the cost: a zero-spend budget alert, actual spend zero, on the AWS Free plan.*

## Part 5: Results (4:15–4:45)
**[SHOW]** `docs/07-real-aws-results.md`, the results tables (or the `evidence\real-aws\drills\20261018\SUMMARY.md`).

*We ran seven real disaster drills on AWS, and all seven passed. After a full regional outage,
users could keep reading the whole time, and writes were working again in 138 to 176 seconds, under our
five-minute target. We lost at most 2.6 seconds of data. Failback lost zero writes. A short health blip
never caused a false failover.*

*Our first gray-failure test took about six minutes, which missed the target. We found the cause,
which was only one health sample per minute, and changed it to five. The next real run took 254 seconds.
Our recovery-time model also predicted 167 seconds against the 176 we measured.*

## Part 6: Conclusion (4:45–5:00)
**[SHOW]** Back to the dashboard or the title slide.

*So the system detects failures intelligently, recovers automatically, protects data, measures its own
recovery time, and runs completely free. Thank you.*

---

### Numbers cheat-sheet (all REAL-AWS unless marked)
| Metric | Value |
|---|---|
| Real drills | 7 run, 7 passed |
| Hard outage: writes back | 138 s and 176 s (target ≤ 300 s) |
| Hard outage: data lost (RPO) | 1.35 s and 2.6 s (target ≤ 60 s) |
| Reads failed during the outage | 0 s |
| Gray failure: writes back | 359–415 s before the fix → **254 s after the fix** |
| Failback data loss | 0 writes (every run) |
| False failovers from blips | 0 |
| Model prediction vs measured | 167 s vs 176 s |
| Cost | $0.00 spent, Free plan |
| Tests | 80 automated tests passing |
| Local dashboard numbers | SIMULATED, scaled timings. Don't quote them as AWS results |
