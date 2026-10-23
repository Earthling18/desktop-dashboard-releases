"""Chunked local transfer; GitHub Actions assembles the single public download ZIP."""
import hashlib
import json
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from release_support import REPOSITORY, encoded, gh, release_record, verify_package, version_key

CHUNK_SIZE = 4 * 1024 * 1024
WORKFLOW = "assemble-release.yml"


def assemble_parts(index, directory, release):
    version_key(index["version"])
    expected = release["windowsX64"]
    if index["schemaVersion"] != 1 or any(index[key] != value for key, value in {"version": release["version"], "file": expected["file"], "size": expected["size"], "sha256": expected["sha256"]}.items()):
        raise ValueError("Transfer index does not match the release")
    parts = index["parts"]
    if not 1 <= len(parts) <= 256 or sum(p["size"] for p in parts) != expected["size"]:
        raise ValueError("Transfer part count or total size is invalid")
    for number, part in enumerate(parts):
        if part["file"] != f"upload-{number:04d}.part" or not 0 < part["size"] <= CHUNK_SIZE or not re.fullmatch(r"[a-f0-9]{64}", part["sha256"]):
            raise ValueError("Transfer part is invalid")
    output = Path(directory) / expected["file"]
    with output.open("wb") as target:
        for part in parts:
            path = Path(directory) / part["file"]
            if path.stat().st_size != part["size"]:
                raise ValueError("Transfer part size mismatch")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != part["sha256"]:
                raise ValueError("Transfer part hash mismatch")
            target.write(data)
    verify_package(output, release)
    return output


def wait_workflow(repository, workflow, title, started):
    for _ in range(30):
        runs = gh("run", "list", "--repo", repository, "--workflow", workflow, "--limit", "20", "--json", "databaseId,displayTitle,createdAt", json_output=True)
        matches = [r for r in runs if r["displayTitle"] == title and datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) >= started.replace(microsecond=0)]
        if matches:
            run_id = matches[0]["databaseId"]
            print(f"Waiting for {title}: run {run_id}", flush=True)
            subprocess.run(["gh", "run", "watch", str(run_id), "--repo", repository, "--exit-status", "--interval", "10"], check=True)
            return run_id
        time.sleep(2)
    raise RuntimeError("GitHub workflow run did not appear")


def upload_archive(tag, package, release):
    assets = release_record(tag)["assets"]
    expected_digest = "sha256:" + release["windowsX64"]["sha256"]
    if any(a["name"] == package.name and a["size"] == package.stat().st_size and a.get("digest") == expected_digest for a in assets):
        print("Verified archive is already uploaded", flush=True)
        return
    with tempfile.TemporaryDirectory(prefix="dashboard-upload-") as temporary:
        directory = Path(temporary)
        parts = []
        with package.open("rb") as stream:
            while data := stream.read(CHUNK_SIZE):
                name = f"upload-{len(parts):04d}.part"
                (directory / name).write_bytes(data)
                parts.append({"file": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        def upload(part):
            if any(a["name"] == part["file"] and a["size"] == part["size"] and a.get("digest") == "sha256:" + part["sha256"] for a in assets):
                return part["file"] + " resumed"
            for attempt in range(3):
                try:
                    gh("release", "upload", tag, str(directory / part["file"]), "--repo", REPOSITORY, "--clobber")
                    return part["file"]
                except RuntimeError:
                    if attempt == 2:raise
                    time.sleep(2 * (attempt + 1))
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = [pool.submit(upload, part) for part in parts]
            for number, completed in enumerate(as_completed(pending), 1):
                print(f"Uploaded part {number}/{len(parts)}: {completed.result()}", flush=True)
        index = {"schemaVersion": 1, "version": release["version"], **{key: release["windowsX64"][key] for key in ["file", "size", "sha256"]}, "parts": parts}
        index_file = directory / "upload-parts.json"
        index_file.write_bytes(encoded(index))
        gh("release", "upload", tag, str(index_file), "--repo", REPOSITORY, "--clobber")
        started = datetime.now(timezone.utc)
        gh("workflow", "run", WORKFLOW, "--repo", REPOSITORY, "--ref", "main", "-f", "version=" + release["version"])
        wait_workflow(REPOSITORY, WORKFLOW, f"Dashboard assemble v{release['version']}", started)


def assemble_draft(version):
    version_key(version)
    tag = "v" + version
    record = release_record(tag)
    if not record["draft"]:
        raise ValueError("Archive assembly is limited to unpublished drafts")
    with tempfile.TemporaryDirectory(prefix="dashboard-assemble-") as temporary:
        from release_support import download_manifest
        release = download_manifest(tag, temporary)
        directory = Path(temporary) / tag
        gh("release", "download", tag, "--repo", REPOSITORY, "--pattern", "upload-*", "--dir", str(directory), "--clobber")
        index_file = directory / "upload-parts.json"
        if index_file.stat().st_size > 256 * 1024:
            raise ValueError("Transfer index is too large")
        index = json.loads(index_file.read_text(encoding="utf-8"))
        package = assemble_parts(index, directory, release)
        gh("release", "upload", tag, str(package), "--repo", REPOSITORY, "--clobber")
        verified = release_record(tag)
        archive = next(a for a in verified["assets"] if a["name"] == package.name)
        if archive["size"] != release["windowsX64"]["size"] or archive.get("digest") != "sha256:" + release["windowsX64"]["sha256"]:
            raise ValueError("GitHub assembled archive digest verification failed")
        for name in [p["file"] for p in index["parts"]] + ["upload-parts.json"]:
            gh("release", "delete-asset", tag, name, "--repo", REPOSITORY, "--yes")
        print(json.dumps({"assembled": version, "sha256": release["windowsX64"]["sha256"], "transferPartsRemoved": len(index["parts"]) + 1}))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser();parser.add_argument("--assemble", required=True)
    assemble_draft(parser.parse_args().assemble)
