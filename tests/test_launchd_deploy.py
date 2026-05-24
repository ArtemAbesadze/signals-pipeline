"""Lint tests for the launchd deployment artifacts (Phase 3.2).

These are sanity checks, not integration tests. We don't actually invoke
`launchctl bootstrap` (it would touch the user's session). What we do:

  - `plutil -lint` parses the rendered plist (after token substitution).
  - Shell scripts have the executable bit set.
  - The plist's ProgramArguments points to a file that exists in the repo.
  - StandardOut/ErrorPath stay inside the project directory.
  - The plist's <Label> matches its filename stem (catches drift).

Skipped cleanly on non-macOS (plutil is macOS-only).
"""

import platform
import re
import shutil
import stat
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy" / "launchd"
TEMPLATE = DEPLOY_DIR / "local.potion-perps-bot.plist.template"
RUN_SH = DEPLOY_DIR / "run.sh"
INSTALL_SH = DEPLOY_DIR / "install.sh"
UNINSTALL_SH = DEPLOY_DIR / "uninstall.sh"
# Phase 4.1 — Telethon forwarder agent
FORWARDER_TEMPLATE = DEPLOY_DIR / "local.potion-perps-forwarder.plist.template"
FORWARDER_RUN_SH = DEPLOY_DIR / "forwarder_run.sh"
FORWARDER_ENTRYPOINT = REPO_ROOT / "scripts" / "telethon_forwarder.py"

# Dummy substitutions for lint-time rendering. Paths just need to be
# syntactically valid; we don't dereference them.
DUMMY_PROJECT_DIR = "/tmp/potion-perps-bot"
DUMMY_PYTHON = "/usr/local/bin/python3"


def _render_plist() -> str:
    text = TEMPLATE.read_text()
    return (
        text
        .replace("{{PROJECT_DIR}}", DUMMY_PROJECT_DIR)
        .replace("{{PYTHON}}", DUMMY_PYTHON)
    )


def _is_executable(path: Path) -> bool:
    return path.exists() and bool(path.stat().st_mode & stat.S_IXUSR)


# ------------------------------------------------------------------
# Existence / permissions
# ------------------------------------------------------------------

def test_artifacts_exist():
    """All five deployment files are present."""
    for f in (TEMPLATE, RUN_SH, INSTALL_SH, UNINSTALL_SH, DEPLOY_DIR / "README.md"):
        assert f.exists(), f"missing deploy artifact: {f}"


def test_shell_scripts_executable():
    """run.sh / install.sh / uninstall.sh must be +x or launchd / users will hit
    `Permission denied` after a fresh clone."""
    for script in (RUN_SH, INSTALL_SH, UNINSTALL_SH):
        assert _is_executable(script), f"{script.name} is not executable"


# ------------------------------------------------------------------
# Plist syntax & content
# ------------------------------------------------------------------

@pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("plutil") is None,
    reason="plutil is macOS-only",
)
def test_rendered_plist_lints(tmp_path):
    """Apple's plutil -lint accepts the rendered plist."""
    rendered = tmp_path / "agent.plist"
    rendered.write_text(_render_plist())
    result = subprocess.run(
        ["plutil", "-lint", str(rendered)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"plutil -lint failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_plist_label_matches_filename():
    """<Label> must match the plist filename stem — launchctl uses the label
    as the service name, and a mismatch causes silent bootstrap failures."""
    text = TEMPLATE.read_text()
    m = re.search(r"<key>Label</key>\s*<string>([^<]+)</string>", text)
    assert m, "no <Label> found in plist template"
    label = m.group(1)
    expected = TEMPLATE.name.replace(".plist.template", "")
    assert label == expected, f"Label {label!r} != filename stem {expected!r}"


def test_plist_program_arguments_target_exists():
    """The first ProgramArguments entry must resolve to a real file in the
    repo (after substituting {{PROJECT_DIR}} with the actual repo root)."""
    text = TEMPLATE.read_text().replace("{{PROJECT_DIR}}", str(REPO_ROOT))
    root = ET.fromstring(text)
    # plist -> dict -> alternating <key>/<value> children
    plist_dict = root.find("dict")
    assert plist_dict is not None
    program_args = None
    children = list(plist_dict)
    for i, el in enumerate(children):
        if el.tag == "key" and el.text == "ProgramArguments":
            program_args = children[i + 1]
            break
    assert program_args is not None and program_args.tag == "array", (
        "ProgramArguments key not followed by <array>"
    )
    first = program_args.find("string")
    assert first is not None and first.text
    target = Path(first.text)
    assert target.exists(), f"ProgramArguments[0] does not exist: {target}"


def test_plist_log_paths_inside_project():
    """StandardOutPath / StandardErrorPath should stay inside the project so
    we don't pollute /var/log or the user's home with surprise files."""
    text = TEMPLATE.read_text()
    for key in ("StandardOutPath", "StandardErrorPath"):
        m = re.search(rf"<key>{key}</key>\s*<string>([^<]+)</string>", text)
        assert m, f"{key} missing from plist"
        path = m.group(1)
        assert path.startswith("{{PROJECT_DIR}}/"), (
            f"{key}={path!r} should start with {{{{PROJECT_DIR}}}}/"
        )


def test_plist_keepalive_is_crash_only():
    """KeepAlive must respect clean exits — otherwise `launchctl bootout`
    fights with launchd to keep the bot restarting."""
    text = TEMPLATE.read_text()
    # SuccessfulExit must be <false/> (restart only on non-zero exit).
    m = re.search(
        r"<key>SuccessfulExit</key>\s*<(true|false)/>",
        text,
    )
    assert m, "KeepAlive.SuccessfulExit missing"
    assert m.group(1) == "false", (
        "KeepAlive.SuccessfulExit must be <false/> — clean exits should not "
        "trigger respawn"
    )


# ------------------------------------------------------------------
# run.sh content
# ------------------------------------------------------------------

def test_run_sh_uses_exec_and_caffeinate():
    """run.sh must `exec caffeinate -i` so SIGTERM reaches Python directly
    (no orphaned shell between launchd and the bot) and idle sleep is
    blocked while the bot runs."""
    text = RUN_SH.read_text()
    assert "exec /usr/bin/caffeinate -i" in text, (
        "run.sh must `exec /usr/bin/caffeinate -i ...` — see README sleep notes"
    )
    assert "main.py" in text


def test_run_sh_cd_to_project_root():
    """run.sh must cd to the project root so relative paths (config/, data/,
    logs/) resolve correctly under launchd's minimal environment."""
    assert 'cd "$(dirname "$0")/../.."' in RUN_SH.read_text()


def test_run_sh_rotates_launchd_log_files():
    """run.sh must rotate launchd.{out,err} on startup (Phase 3.3). Without
    this, launchd's stdout/stderr files grow unboundedly because launchd
    doesn't rotate them itself.

    Must use `cp` + truncate, not `mv` — launchd opens the files before
    exec'ing run.sh, so a rename would orphan the open fd onto the renamed
    inode and the new process's output would land in .1, not the fresh
    file. See deploy/launchd/README.md."""
    text = RUN_SH.read_text()
    assert "logs/launchd.out" in text
    assert "logs/launchd.err" in text

    # Strip comment lines before checking command usage — comments mention
    # `mv` to explain why we DON'T use it.
    code_lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)

    assert "cp " in code, (
        "run.sh must `cp` (not `mv`) launchd logs — open fd would orphan"
    )
    assert "mv " not in code, (
        "run.sh must not `mv` launchd log files — would orphan launchd's "
        "fd onto the renamed inode. See deploy/launchd/README.md."
    )


# ------------------------------------------------------------------
# install.sh / uninstall.sh content sanity
# ------------------------------------------------------------------

def test_install_sh_refuses_old_python():
    """install.sh must version-gate python3 so a stale system Python doesn't
    silently become the launchd interpreter."""
    text = INSTALL_SH.read_text()
    assert "3.10" in text, "install.sh should gate on a minimum Python version"


def test_install_uninstall_are_idempotent():
    """Both scripts must tolerate being re-run — install rebuilds, uninstall
    no-ops when nothing is loaded."""
    install = INSTALL_SH.read_text()
    uninstall = UNINSTALL_SH.read_text()
    # `bootout` failure must be swallowed so re-install on a fresh machine
    # (where the service isn't loaded yet) doesn't abort.
    assert "bootout" in install and "|| true" in install
    assert "bootout" in uninstall and "|| true" in uninstall


# ------------------------------------------------------------------
# Phase 4.1 — Telethon forwarder agent
# ------------------------------------------------------------------

def test_forwarder_artifacts_exist():
    """Forwarder needs its own plist template, wrapper, and entrypoint."""
    for f in (FORWARDER_TEMPLATE, FORWARDER_RUN_SH, FORWARDER_ENTRYPOINT):
        assert f.exists(), f"missing forwarder artifact: {f}"


def test_forwarder_run_sh_executable():
    assert _is_executable(FORWARDER_RUN_SH), (
        f"{FORWARDER_RUN_SH.name} is not executable"
    )


@pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("plutil") is None,
    reason="plutil is macOS-only",
)
def test_rendered_forwarder_plist_lints(tmp_path):
    text = FORWARDER_TEMPLATE.read_text().replace(
        "{{PROJECT_DIR}}", DUMMY_PROJECT_DIR,
    ).replace("{{PYTHON}}", DUMMY_PYTHON)
    rendered = tmp_path / "forwarder.plist"
    rendered.write_text(text)
    result = subprocess.run(
        ["plutil", "-lint", str(rendered)], capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"plutil -lint failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_forwarder_plist_label_matches_filename():
    text = FORWARDER_TEMPLATE.read_text()
    m = re.search(r"<key>Label</key>\s*<string>([^<]+)</string>", text)
    assert m, "no <Label> found in forwarder plist template"
    label = m.group(1)
    expected = FORWARDER_TEMPLATE.name.replace(".plist.template", "")
    assert label == expected, f"Label {label!r} != filename stem {expected!r}"


def test_forwarder_plist_keepalive_is_crash_only():
    """Same SuccessfulExit=false rule as the main bot — clean stop shouldn't
    fight with launchd."""
    text = FORWARDER_TEMPLATE.read_text()
    m = re.search(
        r"<key>SuccessfulExit</key>\s*<(true|false)/>", text,
    )
    assert m and m.group(1) == "false", (
        "Forwarder KeepAlive.SuccessfulExit must be <false/>"
    )


def test_forwarder_plist_log_paths_inside_project():
    text = FORWARDER_TEMPLATE.read_text()
    for key in ("StandardOutPath", "StandardErrorPath"):
        m = re.search(rf"<key>{key}</key>\s*<string>([^<]+)</string>", text)
        assert m, f"{key} missing from forwarder plist"
        assert m.group(1).startswith("{{PROJECT_DIR}}/"), (
            f"{key}={m.group(1)!r} must be under {{{{PROJECT_DIR}}}}"
        )


def test_forwarder_log_paths_distinct_from_bot():
    """Bot and forwarder must NOT share StandardOut/ErrorPath — interleaved
    writes from two processes would shred each other's lines."""
    bot_text = TEMPLATE.read_text()
    fwd_text = FORWARDER_TEMPLATE.read_text()
    bot_paths = re.findall(
        r"<key>Standard(?:Out|Error)Path</key>\s*<string>([^<]+)</string>",
        bot_text,
    )
    fwd_paths = re.findall(
        r"<key>Standard(?:Out|Error)Path</key>\s*<string>([^<]+)</string>",
        fwd_text,
    )
    assert set(bot_paths).isdisjoint(set(fwd_paths)), (
        f"bot and forwarder share log paths: {set(bot_paths) & set(fwd_paths)}"
    )


def test_forwarder_run_sh_uses_exec_and_caffeinate():
    text = FORWARDER_RUN_SH.read_text()
    assert "exec /usr/bin/caffeinate -i" in text
    assert "scripts/telethon_forwarder.py" in text


def test_forwarder_run_sh_cd_to_project_root():
    assert 'cd "$(dirname "$0")/../.."' in FORWARDER_RUN_SH.read_text()


def test_forwarder_run_sh_rotates_log_files():
    """Same cp+truncate (not mv) rotation as run.sh — launchd opens the
    output files before exec, so a rename orphans the fd. See the
    block comment in forwarder_run.sh."""
    text = FORWARDER_RUN_SH.read_text()
    assert "logs/forwarder.out" in text
    assert "logs/forwarder.err" in text
    code_lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "cp " in code, "forwarder_run.sh must `cp` (not `mv`) launchd logs"
    assert "mv " not in code, (
        "forwarder_run.sh must not `mv` launchd log files — see run.sh"
    )


def test_install_sh_dispatches_modes():
    """install.sh must accept bot / forwarder / all subcommands so the
    forwarder can be installed without re-touching the bot agent."""
    text = INSTALL_SH.read_text()
    for token in ("bot)", "forwarder)", "all)"):
        assert token in text, f"install.sh missing mode handler: {token!r}"


def test_uninstall_sh_dispatches_modes():
    text = UNINSTALL_SH.read_text()
    for token in ("bot)", "forwarder)", "all)"):
        assert token in text, f"uninstall.sh missing mode handler: {token!r}"


def test_install_sh_warns_on_missing_telethon_session():
    """If the session file is missing the agent will respawn-crash. The
    installer should warn the user about needing the interactive first run."""
    text = INSTALL_SH.read_text()
    assert "telethon_forwarder.py" in text
    assert ".telethon_session" in text
