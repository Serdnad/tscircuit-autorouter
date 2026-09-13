import json
import os
import statistics
import sys
import threading
import subprocess
import time
from pathlib import Path

root = Path.cwd()
results = root / "results"
mode = sys.argv[1]
if mode == "typescript":
    # Export the reference output after its existing solve timer has stopped.
    source = root / "typescript/scripts/benchmark/benchmark-run-task.ts"
    text = source.read_text()
    anchor = "    const viaCount = countTraceVias(traces)"
    assert text.count(anchor) == 1
    text = text.replace(anchor, """    await Bun.write(
          `${process.env.BENCHMARK_TRACE_DIR}/${task.sampleNumber}.json`,
          JSON.stringify(traces),
        )
    """ + anchor)
    source.write_text(text)
    subprocess.run(["git", "diff"], cwd=root / "typescript", stdout=(results / "reference-export.patch").open("w"), check=True)

reports = {}
for name in ([mode] if mode in ["typescript", "rust"] else []):
    repo = root / name
    output = results / name
    (output / "traces").mkdir(parents=True)
    env = dict(os.environ, BENCHMARK_TRACE_DIR=str(output / "traces"))
    command = ["bash", "./benchmark.sh", "--pipeline", "9", "--dataset", "srj18",
               "--effort", "1", "--concurrency", "2", "--sample-timeout", "360s"]
    stop_monitor = threading.Event()
    def monitor():
        while not stop_monitor.wait(30):
            memory = subprocess.check_output(["free", "-m"], text=True)
            print("[runner memory] " + memory, flush=True)
            with (output / "memory.log").open("a") as memory_log:
                memory_log.write(memory)
    threading.Thread(target=monitor, daemon=True).start()
    start = time.monotonic()
    print(f"Starting {name}", flush=True)
    with (output / "run.log").open("w") as log:
        process = subprocess.Popen(command, cwd=repo, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
        code = process.wait()
    stop_monitor.set()
    (output / "run-metadata.json").write_text(json.dumps({
        "command": command, "wallTimeSeconds": time.monotonic() - start, "exitCode": code,
    }, indent=2))
    for filename in ["benchmark-result.json", "benchmark-result.txt", "benchmark-snapshots.html"]:
        if (repo / filename).exists():
            (output / filename).write_bytes((repo / filename).read_bytes())
    if code:
        raise SystemExit(code)
    reports[name] = json.loads((output / "benchmark-result.json").read_text())

if mode != "report":
    raise SystemExit(0)
reports = {name: json.loads((results / name / "benchmark-result.json").read_text())
           for name in ["typescript", "rust"]}

main_sha = (results / "typescript-sha.txt").read_text().strip()
rust_sha = (results / "rust-sha.txt").read_text().strip()
subprocess.run([
    "bun", "controller/scripts/benchmark/same-machine-results.ts",
    "--main-report", str(results / "typescript/benchmark-result.json"),
    "--pr-report", str(results / "rust/benchmark-result.json"),
    "--main-sha", main_sha, "--pr-sha", rust_sha,
    "--repository", "Serdnad/tscircuit-autorouter", "--runner-name", "ubuntu-24.04-arm",
    "--output", str(results / "comparison.md"),
], check=True)
report = (results / "comparison.md").read_text().replace("one Blacksmith job", "one GitHub Actions job")
report = report.replace(f"https://github.com/Serdnad/tscircuit-autorouter/commit/{main_sha}",
                        f"https://github.com/tscircuit/tscircuit-autorouter/commit/{main_sha}")
report += "\n## All boards\n\n| Board | TS | Rust | Speedup | Final routes |\n| --- | ---: | ---: | ---: | --- |\n"
rows = {name: {row["sampleNumber"]: row for row in data["tests"]} for name, data in reports.items()}
assert rows["typescript"].keys() == rows["rust"].keys()
assert len(rows["rust"]) == 16
parity_errors = []
completed = []
for number, ts in sorted(rows["typescript"].items()):
    rs = rows["rust"][number]
    for field in ["didSolve", "didTimeout", "viaCount", "drcErrorCount", "relaxedDrcPassed"]:
        if ts.get(field) != rs.get(field):
            parity_errors.append(f"Board {number}: {field} differs")
    if ts["didSolve"] and rs["didSolve"]:
        equal = ((results / f"typescript/traces/{number}.json").read_bytes() ==
                 (results / f"rust/traces/{number}.json").read_bytes())
        if not equal:
            parity_errors.append(f"Board {number}: final routes differ")
        completed.append((ts["elapsedTimeMs"], rs["elapsedTimeMs"]))
        status = "Identical" if equal else "DIFFERENT"
    else:
        status = "Failed / timed out"
    report += f"| {number} | {ts['elapsedTimeMs']/1000:.2f}s | {rs['elapsedTimeMs']/1000:.2f}s | {ts['elapsedTimeMs']/rs['elapsedTimeMs']:.2f}x | {status} |\n"
if completed:
    ts_mean = statistics.mean(pair[0] for pair in completed)
    rs_mean = statistics.mean(pair[1] for pair in completed)
    report += f"\nCommon completed-board mean: TS {ts_mean/1000:.2f}s; Rust {rs_mean/1000:.2f}s; **{ts_mean/rs_mean:.2f}x**.\n"
report += "\nParity differences: " + ("; ".join(parity_errors) if parity_errors else "none") + "\n"
(results / "comparison.md").write_text(report)
with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
    summary.write(report)
print(report, flush=True)
if parity_errors:
    raise SystemExit("Benchmark completed with parity differences; inspect artifacts")
