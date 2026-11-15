"""The tool test: each tool that runs code runs in a sandbox, and a sandbox
holds no credential and no archive. The tools that are on by default and that
the config denies are not available to a turn.

It runs on the test cluster of CI (tests/kind/up.sh). One turn of the agent
runs the plan `code-tools` of the mock model: each tool that runs code, the
file tools, a call through the tool search, and a write to the path of the
config. Each code tool runs a probe. The test reads the tool results that the
mock received, and runs the same probe in the agent pod as a control: the
probe must find there what it must not find in a sandbox.

A second turn runs the plan `denied-tools`: it asks for each denied tool by
name, offered or not, and then searches for them.

A third turn runs the plan `web-search` in a sandboxed session, as each
session of an agent is: one web search, which must get to the fake search
endpoint of the pod and come back. A sandboxed session uses a search provider
only when it is bundled or a verified official install, so this holds the
official plugins step of the image.
"""

import json
import os
import re
import unittest
import uuid

from support import (
    AGENT_NS,
    REPO,
    FAKE_SECRETS,
    FILE_MARKER,
    agent_turn,
    kx,
    load_mock,
    mock_records,
    parse_probes,
    probe_command,
    sandbox_problems,
)

# The tools that the agent offers to the model in a sandboxed turn, with the
# config of the test. A new tool in a product update fails the test, so that
# a person checks where it runs before it ships.
KNOWN_TOOLS = {
    "apply_patch",
    "dispatch_action",
    "edit",
    "exec",
    "ls",
    "process",
    "read",
    "sessions_yield",
    "tool_describe",
    "tool_search",
    # The search of the web. It runs in the gateway, not in a sandbox: one
    # HTTP call to the search endpoint of the config. It runs no code.
    "web_search",
    "write",
}
# The tools of the product that run code. Each one must be offered and run in
# the sandbox.
CODE_TOOLS = {"exec", "process", "apply_patch", "dispatch_action"}
# Tools of the product that run code or move files outside a sandbox. The tool
# search must not find one.
HOST_TOOLS = {
    "code_execution",
    "file_fetch",
    "file_write",
    "dir_list",
    "dir_fetch",
    "node_inference",
    "browser",
    "canvas",
}


@unittest.skipUnless(
    os.environ.get("IMAGE_TEST_KUBECONFIG"), "no test cluster (tests/kind/up.sh)"
)
class ToolsRunInASandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_mock().PLANS["code-tools"]
        cls.mark = uuid.uuid4().hex[:12]
        cls.agent_host = kx(
            "-n", AGENT_NS, "exec", "agent", "-c", "agent", "--", "hostname"
        ).stdout.strip()
        cls.config_before = cls.config_hash()
        started, cls.turn = agent_turn(
            f"tool-test-{cls.mark}", f"TOOLPLAN=code-tools WRITEMARK={cls.mark}"
        )
        records = [r for r in mock_records(started) if r.get("plan") == "code-tools"]
        cls.last = (
            max(records, key=lambda r: len(r.get("tool_uses", []))) if records else {}
        )
        # The result of each step of the plan, by its index. The product can
        # change the ids of the calls, so the index is the place of the call.
        index = {
            use.get("id"): i for i, use in enumerate(cls.last.get("tool_uses", []))
        }
        cls.results = {}
        for result in cls.last.get("tool_results", []):
            if result.get("id") in index:
                cls.results[index[result["id"]]] = result
        print(f"\nturn: exit {cls.turn.returncode}, answer {cls.last.get('answer')!r}")
        print(f"offered: {sorted(cls.last.get('tools_offered', []))}")
        for i, step in enumerate(cls.plan):
            result = cls.results.get(i, {})
            text = (result.get("text") or "").replace("\n", " | ")
            print(
                f"step {i:2d} {step['tool']:<16} error={result.get('error')} {text[:400]}"
            )

    @staticmethod
    def config_hash():
        out = kx(
            "-n",
            AGENT_NS,
            "exec",
            "agent",
            "-c",
            "agent",
            "--",
            "sha256sum",
            "/home/node/.openclaw/openclaw.json",
        )
        return out.stdout.split()[0]

    def result(self, index):
        self.assertIn(
            index,
            self.results,
            f"no result for step {index} ({self.plan[index]['tool']})",
        )
        return self.results[index]

    def probes(self, *indexes):
        out = []
        for i in indexes:
            out.extend(parse_probes(self.results.get(i, {}).get("text")))
        return out

    def assert_sandboxed(self, probe):
        problems = sandbox_problems(probe, self.agent_host)
        self.assertEqual(
            problems, [], f"a probe did not run in a clean sandbox: {probe}"
        )

    def test_turn_ran_each_step(self):
        self.assertEqual(self.turn.returncode, 0, self.turn.stderr[-3000:])
        self.assertEqual(len(self.last.get("tool_uses", [])), len(self.plan))
        self.assertEqual(self.last.get("answer"), "MOCK-FINAL")

    def test_offered_tools(self):
        offered = set(self.last.get("tools_offered", []))
        self.assertTrue(CODE_TOOLS <= offered, f"not offered: {CODE_TOOLS - offered}")
        self.assertEqual(
            offered - KNOWN_TOOLS,
            set(),
            "a new tool: check where it runs, then add it here",
        )

    def test_exec(self):
        self.assertFalse(self.result(0)["error"], self.result(0)["text"])
        probes = self.probes(0)
        self.assertEqual(len(probes), 1)
        self.assert_sandboxed(probes[0])

    def test_exec_asks_for_the_host(self):
        # host=auto and elevated=true: the call fails, or it runs in the sandbox.
        result = self.result(1)
        probes = self.probes(1)
        if result["error"]:
            self.assertIn("elevated is not available", result["text"])
            self.assertEqual(probes, [])
        for probe in probes:
            self.assert_sandboxed(probe)

    def test_exec_in_the_background_and_process(self):
        for i in (2, 3, 4):
            self.result(i)
        probes = self.probes(2, 3, 4)
        self.assertTrue(probes, "the process tool gave no output of the command")
        for probe in probes:
            self.assert_sandboxed(probe)

    def test_dispatch_action(self):
        self.assertFalse(self.result(10)["error"], self.result(10)["text"])
        probes = self.probes(10)
        self.assertEqual(len(probes), 1)
        self.assert_sandboxed(probes[0])

    def test_file_tools_reach_the_sandbox(self):
        for i in (5, 6, 7, 8):
            self.assertFalse(
                self.result(i)["error"], f"step {i}: {self.result(i)['text']}"
            )
        text = self.result(9)["text"]
        self.assertIn(f"EDIT-MARK {self.mark}", text)
        self.assertIn(f"PATCH-MARK {self.mark}", text)
        host = re.search(r"host=(\S+)", text)
        self.assertIsNotNone(host)
        self.assertEqual(
            host.group(1),
            self.probes(0)[0].get("host"),
            "the files are not in the sandbox of exec",
        )

    def test_config_stays_as_in_git(self):
        self.result(11)
        self.result(12)
        self.assertEqual(self.config_hash(), self.config_before)

    def test_tool_search_finds_no_host_tool(self):
        text = self.result(13)["text"]
        found = set(re.findall(r'"name":\s*"([^"]+)"', text))
        print(f"tool search found: {sorted(found)}")
        self.assertEqual(found & HOST_TOOLS, set())

    def test_no_secret_value_in_any_result(self):
        values = list(FAKE_SECRETS.values()) + [FILE_MARKER]
        for i, result in self.results.items():
            for value in values:
                self.assertNotIn(value, result.get("text", ""), f"step {i}")

    def test_the_probe_finds_the_files_of_the_agent_pod(self):
        # The control: the same probe in the agent pod finds each thing that
        # it must not find in a sandbox. So a zero in a sandbox means a clean
        # sandbox, not a probe that can not see.
        out = kx(
            "-n",
            AGENT_NS,
            "exec",
            "agent",
            "-c",
            "agent",
            "--",
            "sh",
            "-c",
            probe_command(),
            timeout=300,
        ).stdout
        probes = parse_probes(out)
        self.assertEqual(len(probes), 1, out)
        probe = probes[0]
        print(f"control probe: {probe}")
        self.assertEqual(probe["host"], self.agent_host)
        self.assertEqual(probe["uid"], "1000")
        for key in (
            "env_secret_values",
            "config_file",
            "state_dirs",
            "archive_dir",
            "archive_files",
            "marker_files",
        ):
            self.assertGreater(int(probe[key]), 0, key)
        for name in FAKE_SECRETS:
            self.assertIn(name, probe["env_names"].split(","))


# The tools of the product that are on by default (bundled plugins with
# `enabledByDefault`) and that the config of each agent denies by name:
# `code_execution` (plugin xai) and the file transfer tools (plugin
# file-transfer).
DENIED_TOOLS = {"code_execution", "file_fetch", "file_write", "dir_list", "dir_fetch"}


class TheConfigDeniesTheDefaultTools(unittest.TestCase):
    def test_the_deny_list(self):
        config = json.loads((REPO / "tests" / "config" / "openclaw.json").read_text())
        deny = set(config.get("tools", {}).get("deny", []))
        self.assertEqual(DENIED_TOOLS - deny, set(), "not in tools.deny")
        self.assertEqual(set(load_mock().DENIED_TOOLS), DENIED_TOOLS)
        self.assertTrue(DENIED_TOOLS <= HOST_TOOLS)


@unittest.skipUnless(
    os.environ.get("IMAGE_TEST_KUBECONFIG"), "no test cluster (tests/kind/up.sh)"
)
class DeniedToolsAreNotAvailable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_mock().PLANS["denied-tools"]
        cls.mark = uuid.uuid4().hex[:12]
        started, cls.turn = agent_turn(
            f"denied-test-{cls.mark}", f"TOOLPLAN=denied-tools WRITEMARK={cls.mark}"
        )
        records = [r for r in mock_records(started) if r.get("plan") == "denied-tools"]
        cls.records = records
        cls.last = (
            max(records, key=lambda r: len(r.get("tool_uses", []))) if records else {}
        )
        index = {
            use.get("id"): i for i, use in enumerate(cls.last.get("tool_uses", []))
        }
        cls.results = {}
        for result in cls.last.get("tool_results", []):
            if result.get("id") in index:
                cls.results[index[result["id"]]] = result
        print(
            f"\ndenied turn: exit {cls.turn.returncode}, answer {cls.last.get('answer')!r}"
        )
        for i, step in enumerate(cls.plan):
            result = cls.results.get(i, {})
            text = (result.get("text") or "").replace("\n", " | ")
            print(
                f"step {i:2d} {step['tool']:<16} error={result.get('error')} {text[:300]}"
            )

    def test_the_turn_ran_each_step(self):
        self.assertEqual(self.turn.returncode, 0, self.turn.stderr[-3000:])
        self.assertEqual(len(self.last.get("tool_uses", [])), len(self.plan))
        self.assertEqual(self.last.get("answer"), "MOCK-FINAL")

    def test_not_offered(self):
        for record in self.records:
            offered = set(record.get("tools_offered", []))
            self.assertEqual(offered & DENIED_TOOLS, set())

    def test_a_call_by_name_fails(self):
        # The product answers a call to a tool that is not in the turn with
        # an error, and runs nothing.
        for i, step in enumerate(self.plan):
            if step["tool"] not in DENIED_TOOLS:
                continue
            with self.subTest(tool=step["tool"]):
                self.assertIn(i, self.results, f"no result for {step['tool']}")
                result = self.results[i]
                self.assertTrue(result["error"], result["text"])
                self.assertRegex(result["text"], r"(?i)not found|not available|denied")
                self.assertNotIn("PROBE-BEGIN", result["text"])
                self.assertNotIn(FILE_MARKER, result["text"])
                for value in FAKE_SECRETS.values():
                    self.assertNotIn(value, result["text"])

    def test_the_tool_search_finds_none(self):
        i = next(i for i, s in enumerate(self.plan) if s["tool"] == "tool_search")
        self.assertIn(i, self.results, "no result for the tool search")
        text = self.results[i]["text"]
        found = set(re.findall(r'"name":\s*"([^"]+)"', text))
        print(f"tool search found: {sorted(found)}")
        self.assertFalse(self.results[i]["error"], text)
        self.assertEqual(found & DENIED_TOOLS, set())


@unittest.skipUnless(
    os.environ.get("IMAGE_TEST_KUBECONFIG"), "no test cluster (tests/kind/up.sh)"
)
class WebSearchInASandboxedSession(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_mock().PLANS["web-search"]
        cls.mark = uuid.uuid4().hex[:12]
        started, cls.turn = agent_turn(
            f"search-test-{cls.mark}", f"TOOLPLAN=web-search WRITEMARK={cls.mark}"
        )
        records = [r for r in mock_records(started) if r.get("plan") == "web-search"]
        cls.last = (
            max(records, key=lambda r: len(r.get("tool_uses", []))) if records else {}
        )
        index = {
            use.get("id"): i for i, use in enumerate(cls.last.get("tool_uses", []))
        }
        cls.results = {}
        for result in cls.last.get("tool_results", []):
            if result.get("id") in index:
                cls.results[index[result["id"]]] = result
        cls.seen = []
        out = kx("-n", AGENT_NS, "logs", "agent", "-c", "fake-search").stdout
        for line in out.splitlines():
            try:
                cls.seen.append(json.loads(line))
            except ValueError:
                continue
        print(
            f"\nsearch turn: exit {cls.turn.returncode}, answer {cls.last.get('answer')!r}"
        )
        for i, step in enumerate(cls.plan):
            result = cls.results.get(i, {})
            text = (result.get("text") or "").replace("\n", " | ")
            print(
                f"step {i:2d} {step['tool']:<16} error={result.get('error')} {text[:300]}"
            )

    def test_the_session_is_sandboxed(self):
        out = kx(
            "-n",
            AGENT_NS,
            "exec",
            "agent",
            "-c",
            "agent",
            "--",
            "node",
            "/app/openclaw.mjs",
            "sandbox",
            "explain",
            "--json",
        ).stdout
        explain = json.loads(out[out.index("{") :])
        self.assertIs(explain["sandbox"]["sessionIsSandboxed"], True)
        self.assertEqual(explain["sandbox"]["mode"], "all")

    def test_the_search_comes_back(self):
        self.assertEqual(self.turn.returncode, 0, self.turn.stderr[-3000:])
        i = next(i for i, s in enumerate(self.plan) if s["tool"] == "dispatch_action")
        self.assertIn(i, self.results, "no result for the web search")
        result = self.results[i]
        self.assertFalse(result["error"], result["text"])
        self.assertIn(f"FAKE-SNIPPET-tnwks fake query {self.mark}", result["text"])
        self.assertNotIn(FAKE_SECRETS["BRAVE_API_KEY"], result["text"])

    def test_the_fake_endpoint_got_the_query_and_the_key(self):
        self.assertIn(
            {
                "path": "/res/v1/web/search",
                "q": f"tnwks fake query {self.mark}",
                "key": True,
            },
            self.seen,
        )


if __name__ == "__main__":
    unittest.main()
