"""Run in the existing CC release repository's Actions with its OSS secrets."""
import argparse
import json
import os
import tempfile
import urllib.request
import uuid
from pathlib import Path
from release_support import (KEEP, OSS_BASE, OSS_PREFIX, REPOSITORY, download_manifest, encoded,
                             gh, obsolete_objects, published_releases, verify_package, version_key)


def public_json(name):
    url = f"{OSS_BASE}/{name}?verify={uuid.uuid4().hex}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}), timeout=30) as response:
        content = response.read(512 * 1024 + 1)
        if len(content) > 512 * 1024:
            raise ValueError("Public release metadata is too large")
        return json.loads(content)


def mirror(version, apply):
    version_key(version)
    records = published_releases()
    kept = records[:KEEP]
    if not kept or kept[0]["tag_name"] != "v" + version:
        raise ValueError("Only the newest published stable release can update the channel")
    with tempfile.TemporaryDirectory(prefix="dashboard-mirror-") as temporary:
        releases = [download_manifest(r["tag_name"], temporary) for r in kept]
        if not apply:
            print(json.dumps({"dryRun": True, "keep": [r["version"] for r in releases], "prefix": OSS_PREFIX}))
            return
        import oss2
        bucket_name = os.environ["OSS_BUCKET"]
        endpoint = os.environ["OSS_ENDPOINT"].removeprefix("https://").removeprefix("http://").rstrip("/")
        if f"https://{bucket_name}.{endpoint}/{OSS_PREFIX.rstrip('/')}" != OSS_BASE:
            raise ValueError("OSS destination differs from the configured public download channel")
        auth = oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"])
        bucket = oss2.Bucket(auth, "https://" + endpoint, bucket_name, connect_timeout=30)
        for release in releases:
            package = release["windowsX64"]
            version_prefix = f"{OSS_PREFIX}v{release['version']}/"
            key = version_prefix + package["file"]
            upload = True
            try:
                head = bucket.head_object(key)
                upload = int(head.headers["Content-Length"]) != package["size"] or head.headers.get("x-oss-meta-sha256") != package["sha256"]
            except oss2.exceptions.NotFound:
                pass
            if upload:
                directory = Path(temporary) / ("v" + release["version"])
                gh("release", "download", "v" + release["version"], "--repo", REPOSITORY, "--pattern", package["file"], "--dir", str(directory), "--clobber")
                path = directory / package["file"]
                verify_package(path, release)
                bucket.put_object_from_file(key, str(path), headers={"Content-Type": "application/zip", "Content-Disposition": f'attachment; filename="{package["file"]}"', "Cache-Control": "public,max-age=31536000,immutable", "x-oss-meta-sha256": package["sha256"]})
            bucket.put_object(version_prefix + "release.json", encoded(release), headers={"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache"})
            bucket.put_object(version_prefix + "SHA256SUMS.txt", (package["sha256"] + "  " + package["file"] + "\n").encode("ascii"), headers={"Content-Type": "text/plain"})
            with urllib.request.urlopen(urllib.request.Request(package["aliyun"], method="HEAD"), timeout=30) as response:
                if int(response.headers["Content-Length"]) != package["size"]:
                    raise ValueError("Public mirror package verification failed")
        catalog = {"schemaVersion": 1, "releases": releases}
        headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache,max-age=0,must-revalidate"}
        bucket.put_object(OSS_PREFIX + "releases.json", encoded(catalog), headers=headers)
        bucket.put_object(OSS_PREFIX + "latest.json", encoded(releases[0]), headers=headers)
        if public_json("releases.json") != catalog or public_json("latest.json") != releases[0]:
            raise ValueError("Public channel verification failed; old packages were retained")
        kept_versions = {r["version"] for r in releases}
        keys = [obj.key for obj in oss2.ObjectIteratorV2(bucket, prefix=OSS_PREFIX)]
        obsolete = obsolete_objects(keys, kept_versions)
        for start in range(0, len(obsolete), 1000):
            bucket.batch_delete_objects(obsolete[start:start + 1000])
        remaining = [obj.key for obj in oss2.ObjectIteratorV2(bucket, prefix=OSS_PREFIX)]
        if obsolete_objects(remaining, kept_versions):
            raise ValueError("OSS retention cleanup was incomplete")
        print(json.dumps({"verified": True, "kept": sorted(kept_versions, key=version_key, reverse=True), "removedObjects": len(obsolete), "prefix": OSS_PREFIX}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--apply", action="store_true")
    options = parser.parse_args()
    mirror(options.version, options.apply)
