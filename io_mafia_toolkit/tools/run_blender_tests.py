"""Drive :mod:`blender_tests` with one Blender process per model.

Repeatedly importing into a single ``--background`` session eventually corrupts
Blender's memory and crashes the process, regardless of how carefully the scene
is cleared in between. That is a quirk of driving Blender headless in a loop,
not something the addon controls — a person clicking File > New gets a proper
managed reset instead. Giving each model its own process sidesteps it entirely
and has the side benefit that one bad model cannot take the whole run down.

Usage (plain Python, not Blender)::

    python io_mafia_toolkit/tools/run_blender_tests.py \
        --blender "C:/Program Files/Blender Foundation/Blender 5.1/blender.exe" \
        --models  "D:/Hry/Mafia Editovani/models" \
        --maps    "D:/Hry/Mafia Editovani/maps" \
        --limit 50
"""

import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "blender_tests.py")
#: The last line a finished run of the suite prints.
_TALLY = re.compile(r"^\d+ passed, \d+ failed$")


def collect_models(source, limit):
    if os.path.isfile(source):
        models = [l.strip() for l in open(source, encoding="utf-8") if l.strip()]
    else:
        models = sorted(os.path.join(source, f) for f in os.listdir(source)
                        if f.lower().endswith(".4ds"))
    return models[:limit] if limit else models


def run_one(blender, model, maps, out_dir, suite):
    command = [
        blender, "--background", "--factory-startup", "--python", TESTS, "--",
        "--models", model, "--maps", maps, "--out", out_dir, "--suite", suite,
        "--limit", "1",
    ]
    finished = subprocess.run(command, capture_output=True, text=True,
                              errors="replace")
    output = finished.stdout.splitlines()
    lines = [l for l in output if l[:6] in ("[PASS]", "[FAIL]", "[SKIP]")]
    crashed = "EXCEPTION_ACCESS_VIOLATION" in finished.stdout
    if not lines:
        reason = "blender crashed" if crashed else "no result reported"
        return [f"[FAIL] {os.path.basename(model)}  ({reason})"], False
    # The suite prints its tally last. Without it the run stopped partway -
    # an error in one check ends the script, Blender still exits cleanly, and
    # every check after it silently never ran.
    if not any(_TALLY.match(l) for l in output):
        # A traceback goes to stderr, so look there for the error it ended on.
        errors = [l for l in output + finished.stderr.splitlines()
                  if re.match(r"\w+Error\b", l)]
        reason = ("blender crashed" if crashed
                  else errors[-1] if errors else "no tally reported")
        lines.append(f"[FAIL] {os.path.basename(model)} stopped after "
                     f"{len(lines)} check(s)  ({reason})")
    return lines, not any(l.startswith("[FAIL]") for l in lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", required=True)
    parser.add_argument("--models", required=True)
    parser.add_argument("--maps", default="")
    parser.add_argument("--out", default=os.path.join(HERE, "_test_output"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--regression-dir", default="",
                        help="folder holding the models the regression checks "
                             "use (defaults to --models when that is a folder)")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    models = collect_models(args.models, args.limit)
    print(f"{len(models)} model(s), one Blender process each\n")

    failures = []
    for index, model in enumerate(models, start=1):
        lines, ok = run_one(args.blender, model, args.maps, args.out, "round-trip")
        for line in lines:
            print(f"{index:4}/{len(models)}  {line}", flush=True)
        if not ok:
            failures.extend(l for l in lines if l.startswith("[FAIL]"))

    regression_dir = args.regression_dir or (
        args.models if os.path.isdir(args.models)
        else (os.path.dirname(models[0]) if models else ""))
    print("\nregressions")
    lines, ok = run_one(args.blender, regression_dir, args.maps, args.out,
                        "regression")
    for line in lines:
        print(f"        {line}")
    if not ok:
        failures.extend(l for l in lines if l.startswith("[FAIL]"))

    print(f"\n{len(failures)} failure(s)")
    for line in failures:
        print(f"  {line}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
