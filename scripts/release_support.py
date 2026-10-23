"""Shared release validation and retention policy. Never handles application user data."""
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

REPOSITORY = "Earthling18/desktop-dashboard-releases"
MIRROR_REPOSITORY = "Earthling18/claude-code-launcher"
MIRROR_WORKFLOW = "distribute-dashboard.yml"
OSS_PREFIX = "desktop-dashboard/releases/"
OSS_BASE = "https://cc-launcher-dist.oss-cn-shanghai.aliyuncs.com/" + OSS_PREFIX.rstrip("/")
GITHUB_BASE = f"https://github.com/{REPOSITORY}/releases"
KEEP = 3
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def version_key(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError("Expected a stable major.minor.patch version")
    return tuple(map(int, value.split(".")))


def validate_release(release, expected=None):
    version = release["version"]
    version_key(version)
    if expected is not None and version != expected:
        raise ValueError("Release version does not match its GitHub tag")
    package = release["windowsX64"]
    name = f"DesktopDashboard_{version}_win-x64.zip"
    if package["file"] != name or not isinstance(package["size"], int) or not 0 < package["size"] <= 1024 ** 3:
        raise ValueError("Invalid release package")
    if not re.fullmatch(r"[a-f0-9]{64}", package["sha256"]):
        raise ValueError("Invalid SHA256")
    if package["aliyun"] != f"{OSS_BASE}/v{version}/{name}" or package["github"] != f"{GITHUB_BASE}/download/v{version}/{name}":
        raise ValueError("Unexpected release download location")
    if not isinstance(release["notes"], str) or not isinstance(release["publishedAt"], str):
        raise ValueError("Missing release metadata")
    return release


def keep_releases(releases):
    versions = {}
    for release in releases:
        validate_release(release)
        version = release["version"]
        if version in versions:
            raise ValueError("Duplicate release version")
        versions[version] = release
    return sorted(versions.values(), key=lambda r: version_key(r["version"]), reverse=True)[:KEEP]


def obsolete_objects(keys, kept_versions):
    """Only version folders in our exact namespace can ever be removed."""
    result = []
    for key in keys:
        if not key.startswith(OSS_PREFIX):
            continue
        relative = key[len(OSS_PREFIX):]
        directory, separator, remainder = relative.partition("/")
        if not separator or not directory.startswith("v") or not VERSION.fullmatch(directory[1:]) or not remainder or ".." in remainder.split("/"):
            continue
        if directory[1:] not in kept_versions:
            result.append(key)
    return result


def gh(*arguments, json_output=False):
    environment = os.environ.copy()
    # HTTP/1.1 and bounded retries tolerate intermittent proxy resets.
    environment["GODEBUG"] = (environment.get("GODEBUG", "") + ",http2client=0").lstrip(",")
    retryable = arguments[0] in {"api", "run"} and not any(v in arguments for v in ["--method", "-X"]) or arguments[:2] in {("release", "download"), ("release", "upload")}
    for attempt in range(4):
        process = subprocess.run(["gh", *arguments], text=True, encoding="utf-8", capture_output=True, env=environment)
        if not process.returncode:
            return json.loads(process.stdout) if json_output else process.stdout
        error = process.stderr.strip() or "GitHub operation failed"
        if not retryable or attempt == 3 or not any(value in error.lower() for value in ["eof", "timeout", "connection reset", "tls handshake", "temporary failure"]):
            raise RuntimeError(error)
        time.sleep(2 * (attempt + 1))


def published_releases():
    results = []
    for page in range(1, 101):
        records = gh("api", f"repos/{REPOSITORY}/releases?per_page=100&page={page}", json_output=True)
        for release in records:
            tag = release["tag_name"]
            if not release["draft"] and not release["prerelease"] and tag.startswith("v") and VERSION.fullmatch(tag[1:]):
                results.append(release)
        if len(records) < 100:
            return sorted(results, key=lambda r: version_key(r["tag_name"][1:]), reverse=True)
    raise RuntimeError("Too many releases; refusing incomplete retention cleanup")


def release_record(tag):
    # Drafts do not have a tag ref yet, and GitHub's /releases/tags endpoint can return 404.
    for page in range(1, 101):
        records = gh("api", f"repos/{REPOSITORY}/releases?per_page=100&page={page}", json_output=True)
        match = next((r for r in records if r["tag_name"] == tag), None)
        if match is not None:return match
        if len(records) < 100:break
    raise ValueError("Release was not found")


def download_manifest(tag, directory):
    target = Path(directory) / tag
    target.mkdir(parents=True, exist_ok=True)
    gh("release", "download", tag, "--repo", REPOSITORY, "--pattern", "release.json", "--dir", str(target), "--clobber")
    path = target / "release.json"
    if path.stat().st_size > 512 * 1024:
        raise ValueError("Release metadata is too large")
    return validate_release(json.loads(path.read_text(encoding="utf-8")), tag[1:])


def verify_package(path, release):
    path = Path(path)
    if path.name != release["windowsX64"]["file"] or path.stat().st_size != release["windowsX64"]["size"]:
        raise ValueError("Package name or size does not match release metadata")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != release["windowsX64"]["sha256"]:
        raise ValueError("Package SHA256 verification failed")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
