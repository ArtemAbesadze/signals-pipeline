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
