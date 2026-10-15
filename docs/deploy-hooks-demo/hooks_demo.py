#!/usr/bin/env python3
"""Render deploy-hooks.html: what the deploy hooks do after `git pull`.

Clones the committed HEAD of this repository twice into a scratch dir: an
"upstream" clone that gets the commits, and a "server" clone (named
*-obs, since scriptgen takes the brand from the cwd) with the post-merge
hook installed by `make install_hooks`. Each scenario changes upstream (or
the server), pulls on the server like openqa-trigger-from-obs-update.service
does, and records the hook output and the changes in the project folders.

    python3 docs/deploy-hooks-demo/hooks_demo.py [--output FILE] [--keep]

Needs git, make, flock and xmllint (package libxml2-tools).
"""

import argparse
import hashlib
import html
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from script import validate_xml

FACTORY = "xml/obs/openSUSE:Factory.xml"
ARM = "xml/obs/openSUSE:Factory:ARM.xml"
STAGING = "xml/obs/openSUSE:Factory:Staging.xml"
DEMO = "xml/obs/openSUSE:Factory:Demo.xml"
DEMO_XML = """<openQA
    project_pattern="openSUSE:(?P&lt;version&gt;Factory):Demo:ToTest"
    distri="opensuse"
    archs="x86_64">
    <flavor name="DVD" folder="*product*" iso="1"/>
</openQA>
"""
INITIAL = (
    "openSUSE:Factory:ToTest",
    "openSUSE:Factory:ARM:ToTest",
    "openSUSE:Factory:Staging:B",
    "openSUSE:Factory:Staging:Q",
)
LOCKED = "openSUSE:Factory:ARM:ToTest"


def git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Demo",
        GIT_AUTHOR_EMAIL="demo@example.com",
        GIT_COMMITTER_NAME="Demo",
        GIT_COMMITTER_EMAIL="demo@example.com",
    )
    return env


def project_dirs(root):
    return sorted(
        d.name
        for d in root.iterdir()
        if ":" in d.name and d.is_dir() and not d.is_symlink()
    )


class Demo:
    def __init__(self, work):
        self.work = work
        self.upstream = work / "upstream"
        self.server = work / "server-obs"
        self.env = git_env()
        self.sleeper = None

    def run(self, args, cwd, check=True):
        proc = subprocess.run(
            args,
            cwd=cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if check and proc.returncode:
            raise RuntimeError(f"{args} failed:\n{proc.stdout}")
        return proc

    def git(self, *args):
        return self.run(["git", *args], self.upstream).stdout

    def clean(self, text):
        return text.replace(str(self.work) + "/", "…/").replace(str(self.work), "…")

    def setup(self):
        self.run(["git", "clone", "-q", str(REPO), str(self.upstream)], self.work)
        self.git("checkout", "-q", "-B", "master")
        self.run(
            ["git", "clone", "-q", str(self.upstream), str(self.server)], self.work
        )
        for project in INITIAL:
            (self.server / project).mkdir()
            self.run([sys.executable, "script/scriptgen.py", project], self.server)
        return self.run(["make", "-s", "install_hooks"], self.server).stdout

    # upstream changes

    def edit(self, path, old, new):
        file = self.upstream / path
        text = file.read_text()
        if old not in text:
            raise RuntimeError(f"{path}: {old!r} not found")
        file.write_text(text.replace(old, new, 1))

    def drop_batch(self, path, name):
        file = self.upstream / path
        text = file.read_text()
        new = re.sub(
            rf'\n    <batch name="{name}".*?</batch>',
            "",
            text,
            count=1,
            flags=re.DOTALL,
        )
        if new == text:
            raise RuntimeError(f"{path}: batch {name} not found")
        file.write_text(new)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("show", "--stat", "--format=%h %s", "HEAD").strip()

    def validate(self):
        return self.run(["make", "-s", "validate_xml"], self.upstream, check=False)

    # server actions

    def pull(self):
        proc = self.run(
            ["git", "pull", "--quiet", "--rebase", "origin", "master"],
            self.server,
            check=False,
        )
        return "git pull --quiet --rebase origin master", proc

    def tick(self):
        proc = self.run(["script/deploy-hook.sh", "tick"], self.server, check=False)
        return "script/deploy-hook.sh tick", proc

    def snapshot(self):
        files = {}
        for project in project_dirs(self.server):
            for root, dirs, names in os.walk(self.server / project):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in names:
                    if name == "rsync.lock":
                        continue
                    path = Path(root) / name
                    rel = str(path.relative_to(self.server))
                    files[rel] = hashlib.sha1(path.read_bytes()).hexdigest()
            files.setdefault(project + "/", "")
        return files

    def hold_lock(self, project):
        self.sleeper = subprocess.Popen(["sleep", "600"])
        (self.server / project / "rsync.lock").write_text(f"{self.sleeper.pid}\n")

    def release_lock(self, project):
        if self.sleeper:
            self.sleeper.send_signal(signal.SIGTERM)
            self.sleeper.wait()
            self.sleeper = None
        (self.server / project / "rsync.lock").unlink(missing_ok=True)


def summarize(before, after):
    """[(css class, text)] describing what changed in the project folders."""

    def project(f):
        return f.split("/")[0]

    def batch_dirs(files, projects):
        return {
            str(Path(f).parent)
            for f in files
            if f.count("/") >= 2 and project(f) in projects
        }

    lines = []
    projects_before = {project(f) for f in before}
    projects_after = {project(f) for f in after}
    created = sorted(projects_after - projects_before)
    removed = sorted(projects_before - projects_after)
    if created:
        lines.append(
            ("add", f"created {len(created)} project folder(s): " + ", ".join(created))
        )
    if removed:
        lines.append(
            ("del", f"removed {len(removed)} project folder(s): " + ", ".join(removed))
        )
    common = projects_before & projects_after
    old_batches = batch_dirs(before, common)
    new_batches = batch_dirs(after, common)
    lines.extend(
        ("add", f"created batch folder {d}") for d in sorted(new_batches - old_batches)
    )
    lines.extend(
        ("del", f"removed batch folder {d}") for d in sorted(old_batches - new_batches)
    )
    changed = sorted(
        f
        for f in set(before) & set(after)
        if before[f] != after[f] and project(f) in common
    )
    if changed:
        lines.append(
            ("chg", f"{len(changed)} file(s) with new content: " + ", ".join(changed))
        )
    if not lines:
        lines.append(("same", "no change in the project folders"))
    return lines


def generating(output):
    return sorted(set(re.findall(r"deploy_on_pull: (\S+): generating", output)))


# scenarios


def scenarios():
    """[(title, explanation html, action[, prepare])].

    prepare(demo) changes the server before the snapshot;
    action(demo) -> (commit, command, proc).
    """

    def first_run(d):
        return None, *d.tick()

    def readme(d):
        d.edit("README.md", "## License", "## License\n")
        return d.commit("README: whitespace"), *d.pull()

    def rsynctimeout(d):
        d.edit(FACTORY, '    media1="0">', '    media1="0"\n    rsynctimeout="7200">')
        return d.commit("Factory: longer rsync timeout"), *d.pull()

    def narrow(d):
        d.edit(STAGING, 'range="A..O"', 'range="A..H"')
        return d.commit("Factory:Staging: enable A to H only"), *d.pull()

    def widen(d):
        d.edit(STAGING, 'range="A..H" project="Z"', 'range="A..J" project="Z,Y"')
        return d.commit("Factory:Staging: enable A to J, Y and Z"), *d.pull()

    def new_xml(d):
        (d.upstream / DEMO).write_text(DEMO_XML)
        return d.commit("Add openSUSE:Factory:Demo:ToTest"), *d.pull()

    def drop_wsl(d):
        d.drop_batch(ARM, "wsl")
        return d.commit("Factory:ARM: drop the wsl batch"), *d.pull()

    def customize(d):
        script = d.server / LOCKED / "live" / "read_files.sh"
        script.write_text(
            script.read_text().replace(
                "# GENERATED FILE - DO NOT EDIT", "# hand-edited on the server", 1
            )
        )

    def drop_customized(d):
        d.drop_batch(ARM, "live")
        return d.commit("Factory:ARM: drop the live batch"), *d.pull()

    def no_repeat(d):
        d.edit("README.md", "## License\n", "## License")
        return d.commit("README: whitespace again"), *d.pull()

    def locked(d):
        d.hold_lock(LOCKED)
        d.drop_batch(ARM, "microos-qcow")
        return d.commit("Factory:ARM: drop the microos-qcow batch"), *d.pull()

    def unlocked(d):
        d.release_lock(LOCKED)
        return None, *d.tick()

    def invalid(d):
        d.edit(DEMO, ":Demo:ToTest", ":Demo:ToTest(")
        return d.commit("Demo: broken project_pattern"), *d.pull()

    def fixed(d):
        d.edit(DEMO, ":Demo:ToTest(", ":Demo:ToTest")
        d.edit(DEMO, 'distri="opensuse"', 'distri="microos"')
        return d.commit("Demo: fix the project_pattern, use distri microos"), *d.pull()

    def delete_xml(d):
        (d.upstream / DEMO).unlink()
        return d.commit("Remove openSUSE:Factory:Demo:ToTest"), *d.pull()

    def archive_xml(d):
        (d.upstream / "xml" / "archive" / "obs").mkdir(parents=True)
        d.git("mv", ARM, "xml/archive/obs/")
        return d.commit("Archive openSUSE:Factory:ARM"), *d.pull()

    def generator(d):
        with open(d.upstream / "script" / "cfg.py", "a") as f:
            f.write("\n# a comment\n")
        return d.commit("cfg.py: add a comment"), *d.pull()

    return [
        (
            "First run on an existing deployment",
            (
                "The server has four project folders, generated by hand: "
                "<code>openSUSE:Factory:ToTest</code>, "
                "<code>openSUSE:Factory:ARM:ToTest</code>, "
                "<code>openSUSE:Factory:Staging:B</code> and "
                "<code>openSUSE:Factory:Staging:Q</code>. There is no state yet. "
                "<code>deploy-hook.sh tick</code> regenerates them and creates the "
                "other enabled folders of <code>openSUSE:Factory:Staging.xml</code>: "
                '<code>&lt;projects_enabled range="A..O" project="Z"/&gt;</code> '
                "gives <code>Staging:A</code> to <code>O</code>, <code>Z</code> and "
                "<code>Rings:1-MinimalX</code>. <code>Staging:Q</code> is not enabled: "
                "it is kept and reported once, as it was not enabled before either."
            ),
            first_run,
        ),
        (
            "A pull that changes no XML",
            "Only README.md changes: nothing is generated or removed.",
            readme,
        ),
        (
            "A changed XML",
            "Only the project of the changed XML is regenerated.",
            rsynctimeout,
        ),
        (
            "Staging range narrowed",
            (
                '<code>range="A..O"</code> becomes <code>range="A..H"</code>. The '
                "folders of the changed XML are regenerated, then cleanup removes "
                "<code>Staging:I</code> to <code>Staging:O</code>, which are no "
                "longer enabled. <code>Staging:Q</code> was never enabled and stays."
            ),
            narrow,
        ),
        (
            "Staging range widened, an extra project",
            (
                '<code>range="A..J" project="Z,Y"</code>: <code>Staging:I</code>, '
                "<code>J</code> and <code>Y</code> are created; <code>Z</code> "
                "already exists."
            ),
            widen,
        ),
        (
            "A new XML",
            "The project folder of a new XML with a literal pattern is created.",
            new_xml,
        ),
        (
            "A batch removed",
            "The folder of the removed batch is deleted.",
            drop_wsl,
        ),
        (
            "A batch with a hand-edited script removed",
            (
                "<code>live/read_files.sh</code> was edited on the server: its "
                "<code>GENERATED</code> header is gone, so the folder is kept with a "
                "warning."
            ),
            drop_customized,
            customize,
        ),
        (
            "The next pull",
            "The warning about the kept folder is not repeated.",
            no_repeat,
        ),
        (
            "A batch removed while rsync.sh runs",
            (
                f"<code>{LOCKED}/rsync.lock</code> holds the PID of a running process. "
                "Cleanup skips the project and retries later; the hook logs the "
                "failure, <code>git pull</code> still succeeds."
            ),
            locked,
        ),
        (
            "The next tick after rsync.sh finished",
            "The deferred batch folder is removed.",
            unlocked,
        ),
        (
            "An invalid XML is pulled",
            (
                "Both scripts refuse to act and keep their state. CI rejects such a "
                "commit (see below); this is what happens if one gets through."
            ),
            invalid,
        ),
        (
            "The fix is pulled",
            "All commits since the last good state are processed.",
            fixed,
        ),
        (
            "An XML deleted",
            "Its project folder is removed.",
            delete_xml,
        ),
        (
            "An XML archived",
            (
                "<code>openSUSE:Factory:ARM.xml</code> moves to "
                "<code>xml/archive/obs/</code>, outside <code>xml/obs</code>: for the "
                "hooks it is deleted. Its project folder is removed although its "
                "<code>live</code> batch has a hand-edited script."
            ),
            archive_xml,
        ),
        (
            "A generator change",
            (
                "A change in scriptgen.py, cfg.py or the brand plugin regenerates all "
                "projects; here the content stays the same."
            ),
            generator,
        ),
    ]


def ci_cases():
    """[(title, action)]; action(demo) changes the upstream work tree."""

    def misspelt(d):
        d.edit(FACTORY, '<batch name="base"', '<batch rsynctimout="7200" name="base"')

    def default_batch(d):
        d.edit(FACTORY, '<batch name="wsl"', '<batch name="default"')

    def open_ended(d):
        (d.upstream / DEMO).write_text(DEMO_XML.replace(":Demo:ToTest", ":Demo:.+"))

    def overlap(d):
        (d.upstream / DEMO).write_text(DEMO_XML.replace(":Demo:ToTest", ":Staging:A"))

    def enabled_class(d):
        d.edit(STAGING, 'project="Z,Y"', 'project="Z,Y,99"')

    def enabled_range(d):
        d.edit(STAGING, 'range="A..J"', 'range="A...J"')

    def valid(d):
        (d.upstream / DEMO).write_text(DEMO_XML)

    return [
        ("A misspelt attribute (rsynctimout)", misspelt),
        ("A batch named default", default_batch),
        ("A pattern without derivable project names (Demo:.+)", open_ended),
        ("Two XMLs matching the same project (Staging:A)", overlap),
        ("projects_enabled: a number on the letter class [A-Z]", enabled_class),
        ('projects_enabled: range="A...J"', enabled_range),
        ("A valid new XML", valid),
    ]


# rendering

CSS = """
body { font-family: sans-serif; max-width: 72em; margin: 2em auto;
       padding: 0 1em; color: #222; line-height: 1.4; }
h1 { border-bottom: 2px solid #73ba25; }
h2 { margin-top: 2em; color: #35b9ab; }
code { background: #f2f2f2; padding: 0 .2em; }
pre { background: #1e1e1e; color: #ddd; padding: .7em; overflow-x: auto;
      font-size: 85%; white-space: pre-wrap; }
.scenario { border: 1px solid #ccc; border-radius: 4px; padding: 0 1em 1em;
            margin: 1.5em 0; }
.scenario h3 { margin-bottom: .3em; }
ul.changes { list-style: none; padding-left: 0; }
ul.changes li { padding: .15em .4em; margin: .1em 0; }
.add { background: #e6f4e0; } .del { background: #fbe3e3; }
.chg { background: #fff4d6; } .same { background: #eee; }
.rc0 { color: #2e7d32; font-weight: bold; }
.rc1 { color: #c62828; font-weight: bold; }
table { border-collapse: collapse; font-size: 90%; }
td, th { border: 1px solid #ccc; padding: .2em .5em; vertical-align: top;
         text-align: left; }
"""


def esc(text):
    return html.escape(text, quote=False)


def exit_code(rc):
    return f'exit code <span class="rc{min(rc, 1)}">{rc}</span>'


def render_scenario(n, title, explanation, change, command, proc, summary):
    out = [f'<div class="scenario"><h3>{n}. {esc(title)}</h3><p>{explanation}</p>']
    if change:
        out.append(f"<p>Upstream commit:</p><pre>{esc(change)}</pre>")
    gen = generating(proc.stdout)
    generated = f", {len(gen)} project(s) generated" if gen else ""
    out.append(
        f"<p>On the server: <code>{esc(command)}</code>, "
        f"{exit_code(proc.returncode)}{generated}</p>"
    )
    out.append(f"<pre>{esc(proc.stdout.strip() or '(no output)')}</pre>")
    out.append('<ul class="changes">')
    out.extend(f'<li class="{cls}">{esc(line)}</li>' for cls, line in summary)
    out.append("</ul></div>")
    return "\n".join(out)


def names_table(root):
    """The project folders of each XML of the checkout at root."""
    cwd = os.getcwd()
    os.chdir(root)
    try:
        test_dirs = validate_xml.tracked_test_dirs("obs")
        enabled = validate_xml.validate(["xml/obs"]).enabled
    finally:
        os.chdir(cwd)
    rows = []
    for xml in sorted((root / "xml" / "obs").glob("*.xml")):
        m = re.search(r'project_pattern="([^"]*)"', xml.read_text())
        if not m:
            continue
        pattern = html.unescape(m.group(1))
        rel = f"xml/obs/{xml.name}"
        if rel in enabled:
            names = enabled[rel]
            pattern += "  + <projects_enabled>"
        else:
            names = validate_xml.project_names(pattern, test_dirs)
        if len(names) > 4:
            shown = f"{len(names)}: {names[0]}, {names[1]}, …, {names[-1]}"
        else:
            shown = ", ".join(names) or "none, cannot be derived"
        rows.append(
            f"<tr><td>{esc(xml.name)}</td><td><code>{esc(pattern)}</code></td>"
            f"<td>{esc(shown)}</td></tr>"
        )
    return (
        "<table><tr><th>XML</th><th>project_pattern</th>"
        "<th>project folders</th></tr>\n" + "\n".join(rows) + "\n</table>"
    )


def run_all(demo):
    installed = demo.clean(demo.setup()).strip()
    body = [names_table(demo.upstream), "<h2>Scenarios</h2>"]
    body.append(
        "<p>Server setup: the four project folders are generated with "
        "<code>scriptgen.py</code>, then <code>make install_hooks</code>:</p>"
        f"<pre>{esc(installed)}</pre>"
    )
    for n, (title, explanation, action, *prepare) in enumerate(scenarios(), 1):
        for step in prepare:
            step(demo)
        before = demo.snapshot()
        change, command, proc = action(demo)
        proc.stdout = demo.clean(proc.stdout)
        summary = summarize(before, demo.snapshot())
        body.append(
            render_scenario(n, title, explanation, change, command, proc, summary)
        )
        print(f"{n}. {title}: rc={proc.returncode}", file=sys.stderr)

    body.append("<h2>What CI rejects</h2>")
    body.append(
        "<p><code>make validate_xml</code> (the RELAX NG schema, then "
        "<code>script/validate_xml.py</code>) runs in the CI unit job. Each case "
        "is a change on top of upstream:</p>"
    )
    for title, action in ci_cases():
        action(demo)
        proc = demo.validate()
        demo.git("checkout", "-q", ".")
        demo.git("clean", "-q", "-fd", "xml")
        body.append(
            f'<div class="scenario"><h3>{esc(title)}</h3>'
            f"<p>{exit_code(proc.returncode)}</p>"
            f"<pre>{esc(demo.clean(proc.stdout).strip())}</pre></div>"
        )
        print(f"CI: {title}: rc={proc.returncode}", file=sys.stderr)
    return body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", default=str(HERE / "deploy-hooks.html"))
    parser.add_argument("--keep", action="store_true", help="keep the scratch dir")
    args = parser.parse_args(argv)

    os.chdir(REPO)
    head = subprocess.run(
        ["git", "log", "-1", "--format=%h %s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    work = Path(tempfile.mkdtemp(prefix="hooks-demo-"))
    demo = Demo(work)
    try:
        body = run_all(demo)
    finally:
        if demo.sleeper:
            demo.release_lock(LOCKED)
        if args.keep:
            print(f"kept {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>openqa-trigger-from-obs: deploy hooks</title>
<style>{CSS}</style></head><body>
<h1>Deploy hooks: what happens after <code>git pull</code></h1>
<p>After every <code>git pull</code> of the checkout, the git
<code>post-merge</code> hook <code>script/deploy-hook.sh</code> runs
<code>script/deploy_on_pull.py</code> (create and regenerate project folders)
and <code>script/cleanup_on_deploy.py</code> (remove folders that no XML wants
any more). Generated by <code>docs/deploy-hooks-demo/hooks_demo.py</code> from
commit <code>{esc(head)}</code> on {date}: it runs the real scripts against
scratch clones; nothing talks to OBS.</p>
<h2>Project folders per XML</h2>
<p>An XML with <code>&lt;projects_enabled range="X..Y" project="V,W"/&gt;</code>
lists its folders: each value replaces the one character class of
<code>project_pattern</code>. They are created whenever the XML changes, and
removed when a change drops them. Without the element, the folder is the
literal <code>project_pattern</code>, or the matching
<code>t/obs/&lt;Project&gt;</code> folders, created when the XML is added.
Deleting or archiving an XML removes its folders, even hand-edited ones.</p>
{chr(10).join(body)}
</body></html>
"""
    Path(args.output).write_text(page)
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
