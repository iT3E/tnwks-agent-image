"""Unit tests of bin/image-age, with recorded registry answers.

tests/image-age/ has answers of ghcr.io for openclaw/openclaw, recorded on
2026-11-14 and trimmed: an excerpt of the tag list, the image index of
2026.11.3, its linux/amd64 manifest (the config only) and its config (the
`created` time only). A fake registry serves these shapes for each tag of a
test, with the `created` time of the test. No test uses the network.

Run: cd tests && python3 -m unittest -v test_image_age
"""

import copy
import datetime as dt
import email.message
import importlib.machinery
import importlib.util
import json
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RECORDED = REPO / "tests" / "image-age"
NOW = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.timezone.utc)
CHALLENGE = (
    'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
    'scope="repository:openclaw/openclaw:pull"'
)
BASE = "https://ghcr.io/v2/openclaw/openclaw"


def load_script():
    loader = importlib.machinery.SourceFileLoader(
        "image_age", str(REPO / "bin" / "image-age")
    )
    spec = importlib.util.spec_from_loader("image_age", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


image_age = load_script()


def recorded(name):
    return json.loads((RECORDED / f"{name}.json").read_text())


def headers(**values):
    message = email.message.Message()
    for key, value in values.items():
        message[key.replace("_", "-")] = value
    return message


def ago(days):
    """A `created` time exactly `days` days before NOW (RFC 3339, UTC)."""
    return (NOW - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeRegistry:
    """ghcr.io for one image: the auth challenge, tag pages and images.

    `images` maps a tag to the `created` time of its config. `extra_tags`
    are tags with no image (variants and pre-releases the check must not
    open). `fail` is a list of HTTP statuses (or exceptions) for the next
    requests, before the normal answers.
    """

    def __init__(self, images, extra_tags=(), page_size=None, fail=()):
        self.images = images
        self.tags = list(extra_tags) + list(images)
        self.page_size = page_size
        self.fail = list(fail)
        self.requests = []
        self.routes = {}
        for n, (tag, created) in enumerate(images.items()):
            index = copy.deepcopy(recorded("index"))
            manifest = copy.deepcopy(recorded("manifest"))
            config = copy.deepcopy(recorded("config"))
            image_digest = f"sha256:{n:064x}"
            config_digest = f"sha256:{n + 1000:064x}"
            for entry in index["manifests"]:
                if entry.get("platform") == {"architecture": "amd64", "os": "linux"}:
                    entry["digest"] = image_digest
            manifest["config"]["digest"] = config_digest
            if created is None:
                del config["created"]
            else:
                config["created"] = created
            self.routes[f"{BASE}/manifests/{tag}"] = index
            self.routes[f"{BASE}/manifests/{image_digest}"] = manifest
            self.routes[f"{BASE}/blobs/{config_digest}"] = config

    def _tag_page(self, url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        size = self.page_size or int(query["n"][0])
        start = 0
        if "last" in query:
            start = self.tags.index(query["last"][0]) + 1
        page = self.tags[start : start + size]
        extra = {}
        if start + size < len(self.tags):
            last = urllib.parse.quote(page[-1])
            extra["Link"] = (
                f'</v2/openclaw/openclaw/tags/list?last={last}&n={size}>; rel="next"'
            )
        body = {"name": "openclaw/openclaw", "tags": page}
        return 200, headers(**extra), json.dumps(body).encode()

    def get(self, url, request_headers):
        self.requests.append(url)
        if self.fail:
            failure = self.fail.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure, headers(), b"{}"
        if url.startswith("https://ghcr.io/token?"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            assert query["scope"] == ["repository:openclaw/openclaw:pull"], query
            return 200, headers(), json.dumps({"token": "anonymous"}).encode()
        if request_headers.get("Authorization") != "Bearer anonymous":
            return 401, headers(WWW_Authenticate=CHALLENGE), b"{}"
        if url.startswith(f"{BASE}/tags/list"):
            return self._tag_page(url)
        if url in self.routes:
            return 200, headers(), json.dumps(self.routes[url]).encode()
        return 404, headers(), b"{}"


class ImageAgeTest(unittest.TestCase):
    def run_check(self, pin, fake, max_days=image_age.MAX_AGE_DAYS):
        self.sleeps = []
        registry = image_age.Registry(get=fake.get, sleep=self.sleeps.append)
        return image_age.check(pin, registry, NOW, max_days)

    def test_recorded_tags_have_no_newer_stable_release(self):
        """The recorded excerpt: betas and variants only above 2026.11.3."""
        tags = recorded("tags")["tags"]
        self.assertIn("2026.11.5-beta.2", tags)
        self.assertIn("2026.11.3-slim", tags)
        self.assertEqual(image_age.newer_stable(tags, "2026.11.3"), [])
        self.assertEqual(
            image_age.newer_stable(tags, "2026.11.1"), ["2026.11.2", "2026.11.3"]
        )

    def test_recorded_config_time(self):
        created = image_age.parse_time(recorded("config")["created"])
        self.assertEqual(
            created, dt.datetime(2026, 11, 10, 11, 17, 15, 909905, dt.timezone.utc)
        )

    def test_no_newer_release(self):
        fake = FakeRegistry({"2026.11.2": ago(30), "2026.11.3": ago(20)})
        code, line = self.run_check("2026.11.3", fake)
        self.assertEqual(code, image_age.EXIT_OK)
        self.assertEqual(line, "OK: 2026.11.3 is the newest stable release")

    def test_newer_release_at_13_days(self):
        fake = FakeRegistry({"2026.11.3": ago(20), "2026.11.4": ago(13)})
        code, line = self.run_check("2026.11.3", fake)
        self.assertEqual(code, image_age.EXIT_OK, line)
        self.assertIn("13.0 days ago", line)

    def test_newer_release_at_14_days(self):
        fake = FakeRegistry({"2026.11.3": ago(20), "2026.11.4": ago(14)})
        code, line = self.run_check("2026.11.3", fake)
        self.assertEqual(code, image_age.EXIT_OK, line)

    def test_newer_release_at_15_days(self):
        fake = FakeRegistry({"2026.11.3": ago(20), "2026.11.4": ago(15)})
        code, line = self.run_check("2026.11.3", fake)
        self.assertEqual(code, image_age.EXIT_TOO_OLD)
        created = (NOW - dt.timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(
            line,
            "TOO OLD: the pin is 2026.11.3; the first newer stable release "
            f"2026.11.4 came out {created}, 15.0 days ago (limit 14 days, 1 newer)",
        )

    def test_threshold_is_an_input(self):
        fake = FakeRegistry({"2026.11.3": ago(20), "2026.11.4": ago(8)})
        code, _ = self.run_check("2026.11.3", fake, max_days=7)
        self.assertEqual(code, image_age.EXIT_TOO_OLD)

    def test_several_newer_the_first_counts(self):
        fake = FakeRegistry(
            {
                "2026.11.3": ago(40),
                "2026.11.4": ago(20),
                "2026.11.5": ago(10),
                "2026.12.1": ago(1),
            }
        )
        code, line = self.run_check("2026.11.3", fake)
        self.assertEqual(code, image_age.EXIT_TOO_OLD)
        self.assertIn("release 2026.11.4 came out", line)
        self.assertIn("3 newer", line)

    def test_several_newer_first_is_recent(self):
        fake = FakeRegistry(
            {"2026.11.3": ago(40), "2026.11.4": ago(5), "2026.11.5": ago(1)}
        )
        code, line = self.run_check("2026.11.3", fake)
        self.assertEqual(code, image_age.EXIT_OK)
        self.assertIn("release 2026.11.4 came out", line)

    def test_first_is_the_earliest_published(self):
        """A pin on an old line: a later line came out first."""
        fake = FakeRegistry(
            {"2026.9.36": ago(40), "2026.9.37": ago(2), "2026.10.1": ago(30)}
        )
        code, line = self.run_check("2026.9.36", fake)
        self.assertEqual(code, image_age.EXIT_TOO_OLD)
        self.assertIn("release 2026.10.1 came out", line)

    def test_pre_releases_and_variants_ignored(self):
        fake = FakeRegistry(
            {"2026.11.3": ago(20)},
            extra_tags=[
                "2026.11.5-beta.1",
                "2026.11.4-slim",
                "2026.11.4-amd64",
                "2026.11.4rc1",
                "2026.12",
                "2027.1.1.1",
                "latest",
                "main",
                "sha-0a1b2c3",
            ],
        )
        code, line = self.run_check("2026.11.3", fake)
        self.assertEqual(code, image_age.EXIT_OK)
        self.assertEqual(line, "OK: 2026.11.3 is the newest stable release")
        opened = [u for u in fake.requests if "/manifests/" in u]
        self.assertEqual(opened, [])

    def test_pin_not_in_the_list(self):
        fake = FakeRegistry({"2026.11.2": ago(30), "2026.11.4": ago(15)})
        code, line = self.run_check("2026.11.3", fake)
        self.assertEqual(code, image_age.EXIT_TOO_OLD)
        self.assertIn("release 2026.11.4", line)
        self.assertIn("(the pin is not a tag in the registry)", line)

    def test_pin_not_in_the_list_no_newer(self):
        fake = FakeRegistry({"2026.11.2": ago(30)})
        code, line = self.run_check("2026.11.3", fake)
        self.assertEqual(code, image_age.EXIT_OK)
        self.assertIn("not a tag in the registry", line)

    def test_tag_pages_are_followed(self):
        images = {f"2026.10.{n}": ago(60 - n) for n in range(1, 11)}
        images["2026.11.1"] = ago(16)
        fake = FakeRegistry(images, extra_tags=["2026.11.5-beta.1"], page_size=3)
        code, line = self.run_check("2026.10.10", fake)
        self.assertEqual(code, image_age.EXIT_TOO_OLD)
        self.assertIn("release 2026.11.1", line)
        self.assertEqual(sum("/tags/list" in u for u in fake.requests), 5)

    def test_api_error_after_retry(self):
        fake = FakeRegistry({"2026.11.3": ago(20)}, fail=[503, 503])
        with self.assertRaises(image_age.CheckError) as raised:
            self.run_check("2026.11.3", fake)
        self.assertIn("failed twice", str(raised.exception))
        self.assertIn("HTTP 503", str(raised.exception))
        self.assertEqual(self.sleeps, [image_age.RETRY_WAIT_SECONDS])

    def test_api_error_then_success(self):
        fake = FakeRegistry({"2026.11.3": ago(20)}, fail=[OSError("connection reset")])
        code, _ = self.run_check("2026.11.3", fake)
        self.assertEqual(code, image_age.EXIT_OK)
        self.assertEqual(len(self.sleeps), 1)

    def test_missing_created_is_a_check_error(self):
        fake = FakeRegistry({"2026.11.3": ago(20), "2026.11.4": None})
        with self.assertRaises(image_age.CheckError):
            self.run_check("2026.11.3", fake)

    def test_unparseable_pin_is_a_check_error(self):
        fake = FakeRegistry({"2026.11.3": ago(20)})
        for pin in ("latest", "2026.11.3-slim", "2026.11.3rc1"):
            with self.subTest(pin=pin), self.assertRaises(image_age.CheckError):
                self.run_check(pin, fake)

    def test_read_pin(self):
        digest = "sha256:" + "0" * 64
        text = f"FROM ghcr.io/openclaw/openclaw:2026.11.3@{digest} AS product\n"
        self.assertEqual(image_age.read_pin(text), "2026.11.3")
        for bad in ("FROM node:22\n", text + text):
            with self.subTest(text=bad), self.assertRaises(image_age.CheckError):
                image_age.read_pin(bad)

    def test_read_pin_of_this_repo(self):
        pin = image_age.read_pin((REPO / "Dockerfile").read_text())
        self.assertIsNotNone(image_age.parse_version(pin))

    def test_main_prints_a_distinct_check_error(self):
        missing = REPO / "tests" / "image-age" / "no-such-Dockerfile"
        with unittest.mock.patch("builtins.print") as printed:
            code = image_age.main(["--dockerfile", str(missing)])
        self.assertEqual(code, image_age.EXIT_CHECK_ERROR)
        self.assertIn("CHECK ERROR (not an age verdict)", printed.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
