"""The trace test: a marker text does not get to the collector.

It runs on the test cluster of CI (tests/kind/up.sh). One turn of the agent
holds three markers: one in the prompt, one in the input and the output of a
tool call, and one in the answer of the model. The diagnostics plugin of the
product sends the traces of the turn to a collector that prints each span.
The test checks that the spans got there, and that no marker did.
"""

import os
import time
import unittest
import uuid

from support import SUPPORT_NS, agent_turn, kx, mock_records


@unittest.skipUnless(
    os.environ.get("IMAGE_TEST_KUBECONFIG"), "no test cluster (tests/kind/up.sh)"
)
class NoContentInTraces(unittest.TestCase):
    SERVICE = "tnwks-agent-image-test"

    @classmethod
    def setUpClass(cls):
        mark = uuid.uuid4().hex[:12]
        # The bare mark is in each of the three. The session id is not.
        cls.markers = {
            "bare": mark,
            "prompt": f"PROMPTMARK-{mark}",
            "tool": f"TOOLMARK-{mark}",
            "output": f"OUTMARK-{mark}",
        }
        message = (
            f"TOOLPLAN=trace TRACEMARK={mark} OUTMARK={mark} {cls.markers['prompt']}"
        )
        cls.started, cls.turn = agent_turn(
            f"trace-test-{uuid.uuid4().hex[:8]}", message
        )
        records = [r for r in mock_records(cls.started) if r.get("plan") == "trace"]
        cls.last = records[-1] if records else {}
        # The plugin sends spans in batches. Wait for a span of the service
        # that started after the turn, then a little more for the last batch.
        cls.log = ""
        deadline = time.time() + 120
        while time.time() < deadline:
            cls.log = cls.collector_log()
            if f"service.name: Str({cls.SERVICE})" in cls.log and "Span #" in cls.log:
                break
            time.sleep(5)
        time.sleep(15)
        cls.log = cls.collector_log()
        print(f"\nturn: exit {cls.turn.returncode}, answer {cls.last.get('answer')!r}")
        print(f"collector: {cls.log.count('Span #')} spans, {len(cls.log)} bytes")
        names = sorted(
            {
                line.split("Name", 1)[1].strip(" :")
                for line in cls.log.splitlines()
                if "    Name " in line
            }
        )
        print(f"span names: {names}")

    @classmethod
    def collector_log(cls):
        return kx(
            "-n", SUPPORT_NS, "logs", "deploy/otelcol", f"--since-time={cls.started}"
        ).stdout

    def test_the_turn_had_each_marker(self):
        # Each marker was in the turn: the prompt reached the model, the tool
        # ran with the marker, and the answer held its marker.
        self.assertEqual(self.turn.returncode, 0, self.turn.stderr[-3000:])
        self.assertEqual(
            self.last.get("answer"), f"MOCK-FINAL {self.markers['output']}"
        )
        results = self.last.get("tool_results", [])
        self.assertTrue(results, "no tool result")
        self.assertIn(self.markers["tool"], results[0]["text"])
        self.assertIn(self.markers["output"], self.turn.stdout)

    def test_spans_got_to_the_collector(self):
        self.assertIn(f"service.name: Str({self.SERVICE})", self.log)
        self.assertIn("Span #", self.log)

    def test_no_marker_in_the_spans(self):
        for where, marker in self.markers.items():
            self.assertNotIn(marker, self.log, f"the {where} marker is in the traces")


if __name__ == "__main__":
    unittest.main()
