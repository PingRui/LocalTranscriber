import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OpenSourceReadinessTests(unittest.TestCase):
    def test_repository_has_public_delivery_files(self):
        required = (
            ".github/workflows/test.yml",
            ".gitattributes",
            ".gitignore",
            "CONTRIBUTING.md",
            "LICENSE",
            "README.md",
            "SECURITY.md",
            "THIRD_PARTY_NOTICES.md",
            "docs/ARCHITECTURE.md",
            "docs/CODEX_LOCAL_MODEL_ASSIST.md",
            "verify.ps1",
        )
        for relative_path in required:
            self.assertTrue((ROOT / relative_path).is_file(), f"missing {relative_path}")

    def test_source_has_no_user_specific_windows_paths(self):
        paths = [
            ROOT / "app_config.py",
            ROOT / "model_manager.py",
            ROOT / "transcribe.py",
            ROOT / "gui.pyw",
            ROOT / "install.ps1",
            ROOT / "select_and_transcribe.ps1",
            ROOT / "开始本地转写.cmd",
            ROOT / "README.md",
        ]
        forbidden = ("C:\\Users\\", "D:\\AI-Tools", "D:\\AI-Models", "D:\\BaiduNetdiskDownload")
        for path in paths:
            content = path.read_text(encoding="utf-8-sig")
            for value in forbidden:
                self.assertNotIn(value, content, f"{path.name} contains {value}")

    def test_gitignore_excludes_local_and_large_artifacts(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for rule in (
            ".venv/",
            "runtime/",
            ".tmp-*/",
            "*.dll",
            "*.mp4",
            "last_run.log",
            ".env",
            "*.db",
        ):
            self.assertIn(rule, ignore)

    def test_ci_runs_the_same_public_verification_entrypoint(self):
        workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
        verifier = (ROOT / "verify.ps1").read_text(encoding="utf-8-sig")
        self.assertIn(".\\verify.ps1 -Python python", workflow)
        for entrypoint in ("batch_clean.py", "knowledge_space.py", "knowledge_worker.py", "llm_client.py", "trusted_pipeline.py"):
            self.assertIn(entrypoint, verifier)

    def test_portable_launchers_use_project_directory(self):
        launcher = (ROOT / "开始本地转写.cmd").read_text(encoding="utf-8-sig")
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("%~dp0", launcher)
        self.assertIn("$MyInvocation.MyCommand.Path", installer)


if __name__ == "__main__":
    unittest.main()
