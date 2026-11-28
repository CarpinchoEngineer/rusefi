"""Exercise the real matrix script with GitHub-style merge checkouts.

Run with: python3 firmware/bin/test_generate_matrix.py
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


FIRMWARE = Path(__file__).resolve().parents[1]
UI = "java_console/ui/src/main/java/com/rusefi/ConsoleUI.java"
VERSION = "java_tools/version/src/main/java/com/rusefi/UiVersion.java"
FIRMWARE_CHANGE = "firmware/controllers/test.cpp"


class GenerateMatrixTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("GIT_", "PR_"))}
        self.env.update(EVENT_NAME="pull_request", RUN_ATTEMPT="1", COMMIT_MESSAGE="",
                        BOARD_DIR="", BUNDLE_NAME="", SKIP_RATE="", PROJECT_CPU="",
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "-q")
        self.git("config", "user.name", "Matrix Test")
        self.git("config", "user.email", "matrix-test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        for name in ("bin/generate_matrix.sh",
                     "config/boards/common_script_read_meta_env.inc"):
            target = self.root / "firmware" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIRMWARE / name, target)
        for name, rate, cpu in (("uaefi", 0, "ARCH_STM32F4"),
                                ("other", 0, "ARCH_STM32F4"),
                                ("disabled", 100, "ARCH_STM32F4"),
                                ("h7", 0, "ARCH_STM32H7")):
            self.write(f"firmware/config/boards/{name}/meta-info.env",
                       f"BUNDLE_NAME={name}\nSKIP_RATE={rate}\nPROJECT_CPU={cpu}\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Matrix fixture")
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, env=self.env,
                              check=True, text=True, capture_output=True).stdout.strip()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def merge_pr(self, files, message="Test change"):
        self.git("checkout", "-qb", "pr")
        for path in files:
            self.write(path, "test change\n")
        self.git("add", ".")
        self.git("commit", "-qm", message)
        self.env["PR_HEAD_SHA"] = self.git("rev-parse", "HEAD")
        self.env["PR_BASE_SHA"] = self.base
        self.git("checkout", "--detach", self.base)
        self.git("merge", "--no-ff", "-qm", "Synthetic PR merge", "pr")

    def targets(self, **env):
        result = subprocess.run(
            ["bash", "bin/generate_matrix.sh", "config/boards"],
            cwd=self.root / "firmware", env=dict(self.env, **env),
            check=True, text=True, capture_output=True,
        )
        return {entry["build-target"] for entry in json.loads(result.stdout)["include"]}

    def test_ui_only(self):
        self.merge_pr([UI])
        self.assertEqual({"uaefi"}, self.targets())

    def test_ui_and_version(self):
        self.merge_pr([UI, VERSION])
        # Characterizes #10740; intended coverage is only uaefi.
        self.assertEqual({"uaefi", "other"}, self.targets())

    def test_firmware_without_selector(self):
        self.merge_pr([FIRMWARE_CHANGE])
        self.assertEqual({"uaefi", "other"}, self.targets())

    def test_pr_head_commit_selector(self):
        self.merge_pr([FIRMWARE_CHANGE], "Test change only:uaefi")
        # PR events have no head_commit.message; the head commit must be read.
        self.assertEqual({"uaefi", "other"}, self.targets())

    def test_selector_precedence(self):
        self.merge_pr([FIRMWARE_CHANGE], "Test change only:uaefi")
        # Characterizes the PR-default condition overriding explicit selection.
        self.assertEqual({"uaefi", "other"},
                         self.targets(COMMIT_MESSAGE="Test change only:uaefi"))

    def test_selector_on_rerun(self):
        self.merge_pr([FIRMWARE_CHANGE], "Test change only:uaefi")
        self.assertEqual({"uaefi", "other"}, self.targets(RUN_ATTEMPT="2"))

    def test_other_selector_with_ui(self):
        self.merge_pr([UI], "Test change only:other")
        self.assertEqual({"uaefi"}, self.targets())

    def test_unknown_selector(self):
        self.merge_pr([FIRMWARE_CHANGE], "Test change only:docs")
        self.assertEqual({"uaefi", "other"}, self.targets())

    def test_missing_pr_parent_metadata(self):
        self.merge_pr([UI])
        self.assertEqual({"uaefi", "other"}, self.targets(PR_HEAD_SHA=""))

    def test_schedule_and_manual_keep_default_coverage(self):
        for event in ("schedule", "workflow_dispatch"):
            with self.subTest(event=event):
                self.assertEqual({"uaefi", "other"}, self.targets(EVENT_NAME=event))


if __name__ == "__main__":
    unittest.main()
