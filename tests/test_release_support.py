import copy
import hashlib
import json
import sys
import tempfile
import unittest
import importlib.util
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from release_support import (GITHUB_BASE, OSS_BASE, OSS_PREFIX, keep_releases,
                             obsolete_objects, validate_release, verify_package, version_key)
from upload_transport import assemble_parts


def release(version):
    name = f"DesktopDashboard_{version}_win-x64.zip"
    return {"version": version, "publishedAt": "2026-10-23T00:00:00Z", "notes": "fixture", "windowsX64": {
        "file": name, "size": 7, "sha256": hashlib.sha256(b"fixture").hexdigest(),
        "aliyun": f"{OSS_BASE}/v{version}/{name}", "github": f"{GITHUB_BASE}/download/v{version}/{name}"}}


class RetentionTests(unittest.TestCase):
    def test_transfer_assembly_verifies_order_and_final_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            value = release("0.2.0");parts = []
            for number, content in enumerate([b"fix", b"ture"]):
                name = f"upload-{number:04d}.part";(Path(folder) / name).write_bytes(content)
                parts.append({"file": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
            index = {"schemaVersion": 1, "version": value["version"], **{k:value["windowsX64"][k] for k in ["file", "size", "sha256"]}, "parts": parts}
            self.assertEqual(assemble_parts(index, folder, value).read_bytes(), b"fixture")
            (Path(folder) / parts[0]["file"]).write_bytes(b"BAD")
            with self.assertRaises(ValueError):assemble_parts(index, folder, value)
            parts[0]["file"] = "../escape.part"
            with self.assertRaises(ValueError):assemble_parts(index, folder, value)

    def test_keeps_three_by_semantic_version(self):
        selected = keep_releases([release(v) for v in ["1.9.0", "1.10.0", "1.8.0", "2.0.0", "0.9.0"]])
        self.assertEqual([r["version"] for r in selected], ["2.0.0", "1.10.0", "1.9.0"])

    def test_prunes_only_our_old_version_objects(self):
        old = OSS_PREFIX + "v0.1.0/package.zip"
        keys = [old, OSS_PREFIX + "v0.2.0/package.zip", "releases/v0.1.0/CCLauncher.exe",
                "desktop-dashboard-backup/releases/v0.1.0/file", OSS_PREFIX + "latest.json",
                OSS_PREFIX + "v0.1.0/../other", OSS_PREFIX + "vpreview/file"]
        self.assertEqual(obsolete_objects(keys, {"0.2.0"}), [old])

    def test_no_deletion_when_three_versions_are_current(self):
        self.assertEqual(obsolete_objects([OSS_PREFIX + f"v0.{n}.0/app.zip" for n in [1, 2, 3]], {"0.1.0", "0.2.0", "0.3.0"}), [])

    def test_rejects_untrusted_download_locations(self):
        value = release("0.2.0")
        for channel in ["aliyun", "github"]:
            changed = copy.deepcopy(value);changed["windowsX64"][channel] = "https://example.com/other.zip"
            with self.assertRaises(ValueError):validate_release(changed)

    def test_rejects_duplicate_or_prerelease_versions(self):
        with self.assertRaises(ValueError):keep_releases([release("0.2.0"), release("0.2.0")])
        for value in ["0.2.0-beta", "../../releases", "v0.2.0", "01.2.0"]:
            with self.assertRaises(ValueError):version_key(value)

    def test_rejects_tag_mismatch(self):
        with self.assertRaises(ValueError):validate_release(release("0.2.0"), "0.1.0")

    def test_verifies_downloaded_bytes_before_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            value = release("0.2.0");path = Path(folder) / value["windowsX64"]["file"]
            path.write_bytes(b"fixture");verify_package(path, value)
            path.write_bytes(b"changed")
            with self.assertRaises(ValueError):verify_package(path, value)

    def test_mirror_verifies_channel_before_deleting_only_our_old_packages(self):
        self.run_mirror_fixture(False)

    def test_mirror_keeps_old_packages_if_public_verification_fails(self):
        self.run_mirror_fixture(True)

    def test_mirror_retries_a_partial_batch_with_single_object_deletion(self):
        self.run_mirror_fixture(False, partial_batch=True)

    def run_mirror_fixture(self, fail_verification, partial_batch=False):
        spec = importlib.util.spec_from_file_location("mirror_release", Path(__file__).resolve().parents[1] / "scripts" / "mirror-release.py")
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        versions = ["0.4.0", "0.3.0", "0.2.0"]
        releases = [release(v) for v in versions]
        state = {"keys": [OSS_PREFIX + "v0.1.0/old.zip", "releases/v0.1.0/CCLauncher.exe"], "puts": {}, "deleted": [], "events": []}
        class Bucket:
            def head_object(self, key):return SimpleNamespace(headers={"Content-Length": "7", "x-oss-meta-sha256": releases[0]["windowsX64"]["sha256"]})
            def put_object(self, key, value, headers):state["puts"][key] = value;state["events"].append("put:" + key)
            def batch_delete_objects(self, keys):
                if partial_batch:return
                state["events"].append("delete");state["deleted"].extend(keys);state["keys"] = [k for k in state["keys"] if k not in keys]
            def delete_object(self, key):
                state["events"].append("delete");state["deleted"].append(key);state["keys"] = [k for k in state["keys"] if k != key]
        bucket = Bucket()
        fake = SimpleNamespace(Auth=lambda *_: None, Bucket=lambda *_args, **_kwargs: bucket, ObjectIteratorV2=lambda *_args, **_kwargs: [SimpleNamespace(key=k) for k in state["keys"]])
        class Response:
            headers = {"Content-Length": "7"}
            def __enter__(self):return self
            def __exit__(self, *_):pass
        def public(name):
            state["events"].append("public:" + name)
            return {} if fail_verification else json.loads(state["puts"][OSS_PREFIX + name])
        credentials = {"OSS_BUCKET": "cc-launcher-dist", "OSS_ENDPOINT": "oss-cn-shanghai.aliyuncs.com", "OSS_ACCESS_KEY_ID": "fixture", "OSS_ACCESS_KEY_SECRET": "fixture"}
        with patch.dict(sys.modules, {"oss2": fake}), patch.dict("os.environ", credentials), patch.object(module, "published_releases", return_value=[{"tag_name": "v" + v} for v in versions]), patch.object(module, "download_manifest", side_effect=lambda tag, _: release(tag[1:])), patch.object(module, "public_json", side_effect=public), patch.object(module.urllib.request, "urlopen", return_value=Response()):
            if fail_verification:
                with self.assertRaises(ValueError):module.mirror("0.4.0", True)
                self.assertEqual(state["deleted"], [])
            else:
                module.mirror("0.4.0", True)
                self.assertEqual(state["deleted"], [OSS_PREFIX + "v0.1.0/old.zip"])
                self.assertLess(state["events"].index("public:latest.json"), state["events"].index("delete"))


if __name__ == "__main__":unittest.main()
