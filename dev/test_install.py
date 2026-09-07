#!/usr/bin/env python3
"""Exercise `install` – every branch, always into a throwaway directory.

The interesting cases are not the fresh install but the three ways a name can
already be taken: by us, by a link left dangling when the checkout moved, and by
somebody else's command. Only the last is a refusal.
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
INSTALL = os.path.join(ROOT, "install")
fails = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


def run(*args, claude_home=None, env_extra=None):
    env = dict(os.environ)
    if claude_home is not None:
        env["CLAUDE_CONFIG_DIR"] = claude_home
    env.update(env_extra or {})
    r = subprocess.run([INSTALL, *args], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


print("=== install ===\n")
box = tempfile.mkdtemp(prefix="gtools-install-")
try:
    target = os.path.join(box, "bin")          # deliberately absent
    code, out = run(target)
    check("a fresh install creates the target directory", os.path.isdir(target))
    check("it exits clean", code == 0, f"exit {code}")
    for tool in ("gsheets", "gdocs"):
        link = os.path.join(target, tool)
        check(f"{tool} is a symlink into the checkout",
              os.path.islink(link) and os.path.realpath(link) ==
              os.path.realpath(os.path.join(ROOT, tool)))
    # The verification prepends the target, so it proves the link chain and not the
    # lookup. Printing "verified: gsheets" over a PATH that cannot find it is how a
    # working install comes to read as a usable one – and it was the last line out.
    check("an install off PATH does not claim the bare name works",
          "verified: gsheets ->" not in out and "but bare `gsheets`" in out,
          next((l for l in out.splitlines() if "bare" in l), "").strip()[:64])
    check("it warns when the target is not on PATH", "not on your PATH" in out)
    # A profile is read by login and interactive shells; agents, cron and CI use
    # neither, so the profile remedy alone sends them somewhere it cannot help.
    check("and says a profile will not reach a plain `bash -c`",
          "bash -c" in out and ".bashrc" in out)

    on_path = os.path.join(box, "onpath")
    code, out = run(on_path, claude_home=os.path.join(box, "absent"),
                    env_extra={"PATH": on_path + ":/usr/bin:/bin"})
    check("an install on PATH verifies through the bare name",
          code == 0 and "verified: gsheets ->" in out and "but bare" not in out,
          next((l for l in out.splitlines() if l.startswith("verified:")), "")[:64])

    code, out = run(target)
    check("a second run is idempotent", code == 0 and "already points here" in out,
          f"exit {code}")

    # The likeliest collision: a link made before this checkout moved.
    os.remove(os.path.join(target, "gsheets"))
    os.symlink("/nonexistent/old/checkout/gsheets", os.path.join(target, "gsheets"))
    code, out = run(target)
    check("a dangling link is repointed without --force",
          code == 0 and "broken link" in out, f"exit {code}")
    check("and it points here afterwards",
          os.path.realpath(os.path.join(target, "gsheets")) ==
          os.path.realpath(os.path.join(ROOT, "gsheets")))

    # Somebody else's command is the one case that must not be taken silently.
    os.remove(os.path.join(target, "gsheets"))
    foreign = os.path.join(target, "gsheets")
    with open(foreign, "w") as f:
        f.write("#!/bin/sh\necho not ours\n")
    os.chmod(foreign, 0o755)
    code, out = run(target)
    check("a foreign command is refused", code != 0 and "--force" in out, f"exit {code}")
    check("and is left exactly as it was",
          not os.path.islink(foreign) and "not ours" in open(foreign).read())

    code, out = run("--force", target)
    check("--force replaces it", code == 0 and os.path.islink(foreign), f"exit {code}")

    code, out = run("--nonsense", target)
    check("an unknown option is refused, not ignored", code == 2, f"exit {code}")

    # The links can be perfect and the tool still unusable – no venv, no deps. Saying
    # "verified" there hands an agent a green light on an install that cannot work.
    stub = os.path.join(box, "crashing-python")
    with open(stub, "w") as f:
        f.write('#!/bin/sh\n'
                'echo "Traceback (most recent call last):" >&2\n'
                'echo "ModuleNotFoundError: No module named \'googleapiclient\'" >&2\n'
                'exit 1\n')
    os.chmod(stub, 0o755)
    code, out = run(target, env_extra={"GTOOLS_PYTHON": stub})
    check("a crashing tool is not reported as verified",
          code != 0 and "verified:" not in out, f"exit {code}")
    check("and the cause is named, not the traceback's first line",
          "ModuleNotFoundError" in out and "README.md, step 1" in out)

    # google-api-core warns on every call under Python before 3.11, to stderr. Merged
    # into stdout it arrives ahead of the account, and the warning became the thing we
    # called "verified". stdout is the answer; stderr is diagnostics.
    def stub_python(name, body):
        path = os.path.join(box, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    noisy = stub_python("warning-python", (
        'echo "/x/google/api_core/_python_version_support.py:242: FutureWarning: '
        'You are using a non-supported Python version." >&2\n'
        'echo "  warnings.warn(message, FutureWarning)" >&2\n'
        'echo "tester@example.com"\n'
        'echo "project: p-1"\n'))
    code, out = run(target, env_extra={"GTOOLS_PYTHON": noisy})
    verified = next((l for l in out.splitlines() if l.startswith("verified:")), "")
    check("a warning on stderr does not become the verified answer",
          "tester@example.com" in verified and "FutureWarning" not in verified,
          verified[-60:])

    # With nothing on stdout the tool refused – show that refusal, not the warning
    # printed above it.
    refusing = stub_python("refusing-python", (
        'echo "/x/api_core.py:242: FutureWarning: You are using an old Python." >&2\n'
        'echo "no token.json – run auth.py once to authorize" >&2\n'
        'exit 1\n'))
    code, out = run(target, env_extra={"GTOOLS_PYTHON": refusing})
    verified = next((l for l in out.splitlines() if l.startswith("verified:")), "")
    check("with no stdout the tool's own refusal is shown, not the warning",
          "no token.json" in verified and "FutureWarning" not in verified,
          verified[-60:])

    # --claude-md: the only authored file `install` touches, so the cases that
    # matter are the ones where it must NOT write.
    md_home = os.path.join(box, "claude")      # deliberately absent
    md = os.path.join(md_home, "CLAUDE.md")

    code, out = run(target, claude_home=md_home)
    check("a plain run stays quiet with no Claude home", code == 0 and "--claude-md" not in out,
          f"exit {code}")

    os.makedirs(md_home)
    with open(md, "w") as f:
        f.write("# mine\n\n## Something\nkeep me\n")
    code, out = run(target, claude_home=md_home)
    check("a plain run offers the flag once there is one", "--claude-md" in out)
    check("and writes nothing on its own", open(md).read() == "# mine\n\n## Something\nkeep me\n")

    code, out = run("--claude-md", target, claude_home=md_home)
    body = open(md).read()
    check("--claude-md adds the section", code == 0 and "## Google Sheets & Docs" in body,
          f"exit {code}")
    check("between markers, so it can be found and removed",
          body.count("google-docs-editors-agents-cli:begin") == 1
          and body.count("google-docs-editors-agents-cli:end") == 1)
    check("appending, so what was there is untouched", body.startswith("# mine\n\n## Something\nkeep me\n"))

    code, out = run("--claude-md", target, claude_home=md_home)
    check("a second run does not add a second copy",
          code == 0 and "already in" in out and open(md).read() == body, f"exit {code}")

    # Someone else's section on the same subject – ours would be a second answer.
    os.remove(md)
    with open(md, "w") as f:
        f.write("## Sheets\nPrefer gsheets over the connector.\n")
    before = open(md).read()
    code, out = run("--claude-md", target, claude_home=md_home)
    check("an existing mention is left alone", "already mentions" in out)
    check("named by the line, so there is something to act on", "line 2 reads" in out,
          next((l.strip() for l in out.splitlines() if "line 2" in l), ""))
    check("and the file is byte-identical", open(md).read() == before)

    # A last line with no newline would otherwise swallow our opening marker.
    os.remove(md)
    with open(md, "w") as f:
        f.write("## Mine\nno trailing newline")
    code, out = run("--claude-md", target, claude_home=md_home)
    lines = open(md).read().splitlines()
    check("a file with no trailing newline keeps its last line",
          "no trailing newline" in lines and code == 0, f"exit {code}")
    check("and the marker gets its own line",
          any(l.endswith("-->") and l.startswith("<!--") for l in lines))

    code, out = run("--claude-md", os.path.join(box, "bin2"),
                    claude_home=os.path.join(box, "fresh"))
    check("--claude-md creates the Claude home when absent",
          code == 0 and os.path.isfile(os.path.join(box, "fresh", "CLAUDE.md")), f"exit {code}")
finally:
    shutil.rmtree(box, ignore_errors=True)

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
