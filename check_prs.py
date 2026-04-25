import json
import subprocess
import time

with open("pulls.json", encoding="utf-16") as f:
    pulls = json.load(f)

for pr in pulls:
    pr_num = pr["number"]
    pr_title = pr["title"]
    branch = pr["head"]["ref"]
    print(f"Checking PR #{pr_num}: {pr_title} ({branch})")

    try:
        # Get runs for branch, any event, sort by newest
        res = subprocess.run(
            [
                "gh",
                "api",
                f"repos/D-sorganization/Maxwell-Daemon/actions/runs?branch={branch}&per_page=1",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(res.stdout)
        runs = data.get("workflow_runs", [])
        if not runs:
            print("  No PR workflow runs found.")
        else:
            latest_run = runs[0]
            status = latest_run["status"]
            conclusion = latest_run["conclusion"]
            run_updated = latest_run["updated_at"]
            print(
                f"  Latest Run Status: {status}, Conclusion: {conclusion}, Updated: {run_updated}"
            )
            if conclusion == "failure":
                # Fetch jobs
                run_id = latest_run["id"]
                jobs_res = subprocess.run(
                    [
                        "gh",
                        "api",
                        f"repos/D-sorganization/Maxwell-Daemon/actions/runs/{run_id}/jobs",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                jobs_data = json.loads(jobs_res.stdout)
                for j in jobs_data.get("jobs", []):
                    if j["conclusion"] == "failure":
                        print(f"    Failed Job: {j['name']}")
    except subprocess.CalledProcessError as e:
        print(f"  Error fetching runs: {e.stderr}")
    time.sleep(1)
