import ast
import importlib.util
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from chapspan.help_topics import HELP_TOPICS


ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _load_build_release_module():
    spec = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build_release.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _active_markdown_files():
    files = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "packaging" / "AGENTS.md",
    ]
    for name in ("CHANGELOG.md", "CONTRIBUTING.md"):
        path = ROOT / name
        if path.exists():
            files.append(path)
    docs_dir = ROOT / "docs"
    if docs_dir.is_dir():
        files.extend(sorted(docs_dir.glob("*.md")))
        research_dir = docs_dir / "research"
        if research_dir.is_dir():
            files.extend(sorted(research_dir.glob("*.md")))
    return files


class DocsHygieneTests(unittest.TestCase):
    def test_active_docs_have_no_broken_intra_repo_links(self):
        broken = []
        for md_path in _active_markdown_files():
            text = md_path.read_text(encoding="utf-8")
            for match in LINK_RE.finditer(text):
                raw_target = match.group(1).strip().strip("<>")
                target = raw_target.split("#", 1)[0].strip()
                if not target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (md_path.parent / target).resolve()
                try:
                    rel = resolved.relative_to(ROOT)
                except ValueError:
                    # External workspace references (e.g. ../ledger or ../../../../test) live outside this repo.
                    continue
                if rel.parts and rel.parts[0] == ".local-study":
                    continue
                if not resolved.exists():
                    broken.append(f"{md_path.relative_to(ROOT)} -> {raw_target}")
        self.assertEqual(broken, [], f"Found broken intra-repo doc links: {broken}")

    def test_readme_documents_all_resume_citation_statuses(self):
        tree = ast.parse((ROOT / "src" / "chapspan" / "documents.py").read_text(encoding="utf-8"))
        check_draft = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "check_draft"
        )
        statuses = set()
        for node in ast.walk(check_draft):
            if isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == "status" for t in node.targets):
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        statuses.add(node.value.value)
            elif isinstance(node, ast.Dict):
                for key, val in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "status"
                        and isinstance(val, ast.Constant)
                        and isinstance(val.value, str)
                    ):
                        statuses.add(val.value)
        self.assertTrue(statuses)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("span help citations", readme)
        self.assertIn("span help troubleshooting", readme)
        _, help_text = HELP_TOPICS["citations"]
        missing_help = sorted(status for status in statuses if not re.search(rf"\b{re.escape(status)}\b", help_text))
        self.assertEqual(missing_help, [], f"HELP_TOPICS['citations'] missing resume statuses: {missing_help}")

    def test_release_docs_are_isolated_from_dev_docs(self):
        build_release = _load_build_release_module()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        dev_agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        release_agents = (ROOT / "packaging" / "AGENTS.md").read_text(encoding="utf-8")

        self.assertNotEqual(release_agents.strip(), dev_agents.strip())
        self.assertEqual(release_agents.strip(), HELP_TOPICS["agent"][1].strip())

        for doc_name, content in (("README.md", readme), ("packaging/AGENTS.md", release_agents)):
            self.assertNotIn("docs/", content, f"{doc_name} must not link to developer docs/")
            for marker in build_release.FORBIDDEN_RELEASE_DOC_MARKERS:
                self.assertNotIn(marker, content, f"{doc_name} leaked dev marker: {marker}")

        self.assertIn("span help method", readme)
        self.assertIn("span help agent", readme)

        _, method_text = HELP_TOPICS["method"]
        for expected in ("分析五层", "阶段顺序", "矛盾处理", "source_citation", "https://", "仅在用户明确要求时"):
            self.assertIn(expected, method_text)
        self.assertNotIn("必要或用户", method_text)
        self.assertNotIn("必要或用户", release_agents)

    def _write_test_wheel(self, wheel_path: Path, *, version: str, metadata_version: str | None = None,
                          readme_text: str | None = None, stale_help: bool = False):
        meta_ver = metadata_version or version
        body = readme_text if readme_text is not None else (ROOT / "README.md").read_text(encoding="utf-8")
        metadata = (
            "Metadata-Version: 2.4\n"
            "Name: chapspan\n"
            f"Version: {meta_ver}\n"
            "Description-Content-Type: text/markdown\n\n"
            f"{body}"
        )
        with zipfile.ZipFile(wheel_path, "w") as wf:
            wf.writestr(f"chapspan-{meta_ver}.dist-info/METADATA", metadata)
            for py_path in sorted((ROOT / "src" / "chapspan").glob("*.py")):
                text = py_path.read_text(encoding="utf-8")
                if stale_help and py_path.name == "help_topics.py":
                    text = 'HELP_TOPICS = {"quickstart": ("q", "q")}\n'
                wf.writestr(f"chapspan/{py_path.name}", text)

    def test_release_packaging_excludes_docs_and_verifies_consistency(self):
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        for directive in ("include LICENSE", "prune docs", "exclude AGENTS.md", "include packaging/AGENTS.md", "include README.md"):
            self.assertIn(directive, manifest)

        build_release = _load_build_release_module()
        version = build_release.read_package_version(ROOT)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stage_dir = build_release.stage_release_source(ROOT, tmp_path / "stage")
            self.assertFalse((stage_dir / "docs").exists())
            self.assertFalse((stage_dir / "AGENTS.md").exists())
            self.assertFalse((stage_dir / "tests").exists())
            self.assertFalse((stage_dir / "scripts").exists())
            self.assertTrue((stage_dir / "LICENSE").is_file())
            self.assertTrue((stage_dir / "README.md").is_file())
            self.assertTrue((stage_dir / "pyproject.toml").is_file())
            self.assertTrue((stage_dir / "src" / "chapspan" / "__init__.py").is_file())

            good_wheel = tmp_path / f"chapspan-{version}-py3-none-any.whl"
            self._write_test_wheel(good_wheel, version=version)

            zip_path = build_release.assemble_release_zip(ROOT, good_wheel, tmp_path / "release.zip")
            entries = build_release.verify_release_zip(zip_path, expected_version=version, repo_root=ROOT)
            self.assertEqual(entries, sorted([good_wheel.name, "README.md", "AGENTS.md", "LICENSE"]))

            # V03: refusing overwrite preserves existing target bytes
            original_bytes = zip_path.read_bytes()
            with self.assertRaises(FileExistsError):
                build_release.assemble_release_zip(ROOT, good_wheel, zip_path)
            self.assertEqual(zip_path.read_bytes(), original_bytes)

            # V03: main() verifies staged ZIP before touching destination
            spoof_wheel = tmp_path / f"chapspan-{version}-py3-none-any-spoof.whl"
            spoof_named_dir = tmp_path / "spoof_dir"
            spoof_named_dir.mkdir()
            spoof_wheel = spoof_named_dir / f"chapspan-{version}-py3-none-any.whl"
            self._write_test_wheel(spoof_wheel, version=version, metadata_version="0.0.1")
            protected_target = tmp_path / "protected.zip"
            protected_target.write_bytes(b"KEEP_ORIGINAL")
            with self.assertRaises(FileExistsError):
                build_release.main(["--wheel", str(spoof_wheel), "--output", str(protected_target)])
            self.assertEqual(protected_target.read_bytes(), b"KEEP_ORIGINAL")

            # V04: reject spoofed METADATA version and stale wheel missing method/agent
            bad_meta_zip = tmp_path / "bad_meta.zip"
            build_release.assemble_release_zip(ROOT, spoof_wheel, bad_meta_zip)
            with self.assertRaises(ValueError):
                build_release.verify_release_zip(bad_meta_zip, expected_version=version, repo_root=ROOT)

            stale_dir = tmp_path / "stale_dir"
            stale_dir.mkdir()
            stale_wheel = stale_dir / f"chapspan-{version}-py3-none-any.whl"
            self._write_test_wheel(stale_wheel, version=version, stale_help=True)
            stale_zip = tmp_path / "stale.zip"
            build_release.assemble_release_zip(ROOT, stale_wheel, stale_zip)
            with self.assertRaises(ValueError):
                build_release.verify_release_zip(stale_zip, expected_version=version, repo_root=None)
            with self.assertRaises(ValueError):
                build_release.verify_release_zip(stale_zip, expected_version=version, repo_root=ROOT)

            # V05: CRLF line endings in packaging/AGENTS.md and README.md pass verification
            dev_agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
            readme = (ROOT / "README.md").read_text(encoding="utf-8")
            release_agents = (ROOT / "packaging" / "AGENTS.md").read_text(encoding="utf-8")
            crlf_repo = tmp_path / "crlf_repo"
            (crlf_repo / "packaging").mkdir(parents=True)
            (crlf_repo / "src" / "chapspan").mkdir(parents=True)
            for py_path in (ROOT / "src" / "chapspan").glob("*.py"):
                (crlf_repo / "src" / "chapspan" / py_path.name).write_bytes(
                    py_path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8")
                )
            (crlf_repo / "AGENTS.md").write_text(dev_agents, encoding="utf-8")
            (crlf_repo / "LICENSE").write_bytes(
                (ROOT / "LICENSE").read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8")
            )
            (crlf_repo / "README.md").write_bytes(readme.replace("\n", "\r\n").encode("utf-8"))
            (crlf_repo / "packaging" / "AGENTS.md").write_bytes(
                release_agents.replace("\n", "\r\n").encode("utf-8")
            )
            crlf_zip = tmp_path / "crlf.zip"
            build_release.assemble_release_zip(crlf_repo, good_wheel, crlf_zip)
            build_release.verify_release_zip(crlf_zip, expected_version=version, repo_root=crlf_repo)

            # Subprocess help inspection must not rely on caller PYTHONIOENCODING / PYTHONUTF8
            non_utf8_env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
            non_utf8_env["PYTHONIOENCODING"] = "cp936"
            with patch.dict(os.environ, non_utf8_env, clear=True):
                build_release.verify_release_zip(zip_path, expected_version=version, repo_root=ROOT)

    def test_research_docs_have_date_header(self):
        for md_path in sorted((ROOT / "docs" / "research").glob("*.md")):
            head = "\n".join(md_path.read_text(encoding="utf-8").splitlines()[:6])
            self.assertRegex(head, r"20\d{2}-\d{2}-\d{2}", f"Missing date header in {md_path.name}")


if __name__ == "__main__":
    unittest.main()
