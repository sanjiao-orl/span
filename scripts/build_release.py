"""Stage clean source and assemble/verify the local Span release ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_RELEASE_PREFIXES = ("docs/", "tests/", "scripts/", ".local-study/", "dist/")
FORBIDDEN_RELEASE_DOC_MARKERS = (
    "docs/统一方案",
    "docs/开发说明",
    "docs/README.md",
    "docs/设计原则",
    "仅限源码开发环境",
)
WHEEL_VERSION_RE = re.compile(r"^chapspan-([^-]+)-")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_package_version_from_text(init_text: str, origin: str = "__init__.py") -> str:
    for line in normalize_newlines(init_text).splitlines():
        if line.startswith("__version__"):
            _, _, rhs = line.partition("=")
            return rhs.strip().strip("\"'")
    raise RuntimeError(f"Could not find __version__ in {origin}")


def read_package_version(repo_root: Path) -> str:
    """Read __version__ from src/chapspan/__init__.py without importing external deps."""
    init_file = repo_root / "src" / "chapspan" / "__init__.py"
    return read_package_version_from_text(init_file.read_text(encoding="utf-8"), str(init_file))


def default_candidate_name(repo_root: Path, version: str) -> str:
    """Return a source-identifying candidate ZIP filename so existing releases are never clobbered."""
    try:
        sha = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.strip())
        tag = f"{sha}-dirty" if dirty else sha
    except (OSError, subprocess.CalledProcessError):
        tag = "candidate"
    return f"span-{version}-{tag}.zip"


def stage_release_source(repo_root: Path, stage_dir: Path) -> Path:
    """Copy minimal build inputs into stage_dir, strictly excluding docs/ and dev AGENTS.md."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("LICENSE", "pyproject.toml", "README.md", "MANIFEST.in"):
        src = repo_root / filename
        if src.exists():
            shutil.copy2(src, stage_dir / filename)
    shutil.copytree(
        repo_root / "src",
        stage_dir / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
    )
    return stage_dir


def assemble_release_zip(repo_root: Path, wheel_path: Path, output_zip: Path, *, overwrite: bool = False) -> Path:
    """Atomically write the distribution ZIP containing wheel, README.md, release AGENTS.md, and LICENSE."""
    release_agents = repo_root / "packaging" / "AGENTS.md"
    user_readme = repo_root / "README.md"
    license_file = repo_root / "LICENSE"
    if not wheel_path.is_file():
        raise FileNotFoundError(f"Wheel not found: {wheel_path}")
    if not release_agents.is_file():
        raise FileNotFoundError(f"Release AGENTS.md template not found: {release_agents}")
    if not user_readme.is_file():
        raise FileNotFoundError(f"User README.md not found: {user_readme}")
    if not license_file.is_file():
        raise FileNotFoundError(f"LICENSE file not found: {license_file}")
    if output_zip.exists() and not overwrite:
        raise FileExistsError(
            f"交付包已存在，拒绝覆盖：{output_zip}（请改用含来源身份的新文件名，或显式传 --overwrite）"
        )

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".release_", suffix=".zip", dir=output_zip.parent)
    os.close(fd)
    tmp_zip = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(wheel_path, arcname=wheel_path.name)
            zf.write(user_readme, arcname="README.md")
            zf.write(release_agents, arcname="AGENTS.md")
            zf.write(license_file, arcname="LICENSE")
        if overwrite:
            os.replace(tmp_zip, output_zip)
        else:
            tmp_zip.rename(output_zip)
    finally:
        if tmp_zip.exists():
            tmp_zip.unlink()
    return output_zip


def _parse_wheel_metadata(metadata_text: str) -> tuple[dict[str, str], str]:
    headers_block, sep, body = normalize_newlines(metadata_text).partition("\n\n")
    if not sep:
        raise ValueError("Wheel METADATA is missing description separator")
    headers = {}
    for line in headers_block.splitlines():
        if ":" in line and not line[0].isspace():
            key, _, val = line.partition(":")
            headers.setdefault(key.strip(), val.strip())
    return headers, body


def _read_wheel_help_topics(wheel_path: Path) -> dict[str, tuple[str, str]]:
    script = (
        "import json; from chapspan.help_topics import HELP_TOPICS; "
        "print(json.dumps({k: list(v) for k, v in HELP_TOPICS.items()}, ensure_ascii=True))"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(wheel_path)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, "-B", "-c", script],
        capture_output=True, text=True, encoding="utf-8", env=env, check=False,
    )
    if proc.returncode != 0:
        raise ValueError(f"Unable to import chapspan.help_topics from wheel {wheel_path.name}: {proc.stderr.strip()}")
    raw = json.loads(proc.stdout)
    return {key: (val[0], val[1]) for key, val in raw.items()}


def verify_release_zip(zip_path: Path, *, expected_version: str | None = None, repo_root: Path | None = None) -> list[str]:
    """Verify release ZIP layout, wheel metadata/code consistency, and exclusion of dev docs."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = sorted(zf.namelist())
        for name in names:
            norm = name.replace("\\", "/")
            if norm.startswith(FORBIDDEN_RELEASE_PREFIXES):
                raise ValueError(f"Release ZIP contains forbidden developer path: {name}")

        wheels = [name for name in names if name.endswith(".whl") and "/" not in name and "\\" not in name]
        if len(wheels) != 1:
            raise ValueError(f"Expected exactly 1 root wheel in release ZIP, found: {wheels}")
        wheel_name = wheels[0]
        match = WHEEL_VERSION_RE.match(wheel_name)
        if not match:
            raise ValueError(f"Unexpected wheel filename format: {wheel_name}")
        wheel_filename_version = match.group(1)
        if expected_version and wheel_filename_version != expected_version:
            raise ValueError(f"Wheel {wheel_name} does not match expected version {expected_version}")

        expected_names = sorted([wheel_name, "README.md", "AGENTS.md", "LICENSE"])
        if names != expected_names:
            raise ValueError(f"Unexpected release ZIP entries: {names} (expected {expected_names})")

        zip_readme = normalize_newlines(zf.read("README.md").decode("utf-8"))
        zip_agents = normalize_newlines(zf.read("AGENTS.md").decode("utf-8"))
        zip_license = normalize_newlines(zf.read("LICENSE").decode("utf-8"))
        if not zip_license.strip():
            raise ValueError("LICENSE inside release ZIP is empty")
        for doc_name, content in (("README.md", zip_readme), ("AGENTS.md", zip_agents)):
            for marker in FORBIDDEN_RELEASE_DOC_MARKERS:
                if marker in content:
                    raise ValueError(f"{doc_name} inside release ZIP contains dev marker: {marker}")

        if repo_root is not None:
            dev_agents = normalize_newlines((repo_root / "AGENTS.md").read_text(encoding="utf-8"))
            rel_agents = normalize_newlines((repo_root / "packaging" / "AGENTS.md").read_text(encoding="utf-8"))
            repo_readme = normalize_newlines((repo_root / "README.md").read_text(encoding="utf-8"))
            repo_license = normalize_newlines((repo_root / "LICENSE").read_text(encoding="utf-8"))
            if zip_agents == dev_agents:
                raise ValueError("Release ZIP AGENTS.md matches repository dev AGENTS.md")
            if zip_agents != rel_agents:
                raise ValueError("Release ZIP AGENTS.md does not match packaging/AGENTS.md")
            if zip_readme != repo_readme:
                raise ValueError("Release ZIP README.md does not match repository README.md")
            if zip_license != repo_license:
                raise ValueError("Release ZIP LICENSE does not match repository LICENSE")

        with tempfile.TemporaryDirectory() as tmp:
            extracted_whl = Path(zf.extract(wheel_name, path=tmp))
            with zipfile.ZipFile(extracted_whl, "r") as wf:
                wheel_members = wf.namelist()
                for member in wheel_members:
                    norm = member.replace("\\", "/")
                    if norm.startswith(FORBIDDEN_RELEASE_PREFIXES) or norm.endswith("AGENTS.md"):
                        raise ValueError(f"Wheel contains forbidden path: {member}")

                metadata_entries = [m for m in wheel_members if re.fullmatch(r"chapspan-[^/]+\.dist-info/METADATA", m)]
                if len(metadata_entries) != 1:
                    raise ValueError(f"Expected 1 METADATA in wheel, found: {metadata_entries}")
                headers, body = _parse_wheel_metadata(wf.read(metadata_entries[0]).decode("utf-8"))
                if headers.get("Name") != "chapspan":
                    raise ValueError(f"Wheel METADATA Name mismatch: {headers.get('Name')!r}")
                meta_version = headers.get("Version")
                if meta_version != wheel_filename_version:
                    raise ValueError(
                        f"Wheel METADATA Version {meta_version!r} does not match filename version {wheel_filename_version!r}"
                    )
                if expected_version and meta_version != expected_version:
                    raise ValueError(f"Wheel METADATA Version {meta_version!r} != expected {expected_version!r}")
                if body.strip() != zip_readme.strip():
                    raise ValueError("Wheel METADATA description does not match README.md in release ZIP")

                if "chapspan/__init__.py" not in wheel_members:
                    raise ValueError("Wheel is missing chapspan/__init__.py")
                init_ver = read_package_version_from_text(
                    wf.read("chapspan/__init__.py").decode("utf-8"), "wheel chapspan/__init__.py"
                )
                if init_ver != meta_version:
                    raise ValueError(f"Wheel __version__ {init_ver!r} != METADATA Version {meta_version!r}")

                if repo_root is not None:
                    expected_py = {
                        f"chapspan/{path.name}": normalize_newlines(path.read_text(encoding="utf-8"))
                        for path in sorted((repo_root / "src" / "chapspan").glob("*.py"))
                    }
                    actual_py = {
                        member: normalize_newlines(wf.read(member).decode("utf-8"))
                        for member in wheel_members
                        if re.fullmatch(r"chapspan/[^/]+\.py", member)
                    }
                    if set(actual_py) != set(expected_py):
                        raise ValueError(
                            f"Wheel Python modules differ from repo src/chapspan: "
                            f"{sorted(set(actual_py) ^ set(expected_py))}"
                        )
                    mismatched = [name for name, src in expected_py.items() if actual_py[name] != src]
                    if mismatched:
                        raise ValueError(f"Wheel Python source does not match repo src/chapspan: {mismatched}")

            topics = _read_wheel_help_topics(extracted_whl)
            for required_topic in ("quickstart", "method", "agent", "json", "run", "citations", "troubleshooting"):
                if required_topic not in topics:
                    raise ValueError(f"Wheel help_topics is missing required topic: {required_topic}")
            if normalize_newlines(topics["agent"][1]).strip() != zip_agents.strip():
                raise ValueError("Wheel HELP_TOPICS['agent'] does not match AGENTS.md in release ZIP")

        return names


def build_wheel(stage_dir: Path, wheel_dir: Path, *, no_build_isolation: bool = False) -> Path:
    """Build the chapspan wheel from a clean staged directory."""
    wheel_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_dir)]
    if no_build_isolation:
        cmd.append("--no-build-isolation")
    cmd.append(str(stage_dir))
    subprocess.run(cmd, check=True)
    wheels = sorted(wheel_dir.glob("chapspan-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected 1 wheel in {wheel_dir}, found {wheels}")
    return wheels[0]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and verify a clean Span release ZIP.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Repository root directory")
    parser.add_argument("--output", type=Path, help="Output ZIP path (defaults to source-tagged candidate in dist/)")
    parser.add_argument("--wheel", type=Path, help="Optional prebuilt wheel path to package instead of invoking pip wheel")
    parser.add_argument("--no-build-isolation", action="store_true", help="Pass --no-build-isolation to pip wheel")
    parser.add_argument("--overwrite", action="store_true", help="Allow overwriting an existing output ZIP")
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    version = read_package_version(repo_root)
    output_zip = args.output or (repo_root / "dist" / default_candidate_name(repo_root, version))
    if output_zip.exists() and not args.overwrite:
        raise FileExistsError(
            f"交付包已存在，拒绝覆盖：{output_zip}（请更换 --output 文件名，或显式传 --overwrite）"
        )

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        if args.wheel is not None:
            wheel_path = args.wheel.resolve()
        else:
            stage_dir = stage_release_source(repo_root, tmp_path / "src_stage")
            wheel_path = build_wheel(stage_dir, tmp_path / "wheelhouse", no_build_isolation=args.no_build_isolation)

        fd, staged_name = tempfile.mkstemp(prefix=".verify_", suffix=".zip", dir=output_zip.parent)
        os.close(fd)
        staged_zip = Path(staged_name)
        try:
            assemble_release_zip(repo_root, wheel_path, staged_zip, overwrite=True)
            entries = verify_release_zip(staged_zip, expected_version=version, repo_root=repo_root)
            if args.overwrite:
                os.replace(staged_zip, output_zip)
            else:
                staged_zip.rename(output_zip)
        finally:
            if staged_zip.exists():
                staged_zip.unlink()

    print(f"version={version}")
    print(f"output={output_zip}")
    print(f"sha256={sha256_file(output_zip)}")
    print(f"entries={', '.join(entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
