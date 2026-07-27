import time
import unittest

from contextscroll.classifier import Decision
from contextscroll.context_agent import LatestPoint
from contextscroll.protocol import (
    ActivityReport,
    ContextRegistry,
    ContextReport,
    MAX_LINE_BYTES,
    decode,
    decode_activity,
    encode,
)


class ProtocolTests(unittest.TestCase):
    def test_round_trip(self):
        report = ContextReport(
            Decision.NATIVE,
            role="page tab",
            application="Firefox",
            name="ContextScroll",
            x=40,
            y=20,
        )
        self.assertEqual(decode(encode(report)), report)

    def test_oversized_message_is_rejected(self):
        with self.assertRaises(ValueError):
            decode(b"x" * (MAX_LINE_BYTES + 1))

    def test_registry_expires_to_unknown(self):
        registry = ContextRegistry(maximum_age=0.01)
        registry.update("client", ContextReport(Decision.SCROLL))
        time.sleep(0.02)
        self.assertEqual(registry.current().decision, Decision.UNKNOWN)

    def test_activity_report_round_trip_from_daemon(self):
        self.assertEqual(
            decode_activity(b'{"v":1,"type":"activity","active":true}\n'),
            ActivityReport(True),
        )

    def test_invalid_activity_report_is_rejected(self):
        with self.assertRaises(ValueError):
            decode_activity(
                b'{"v":1,"type":"activity","active":"yes"}\n'
            )

    def test_latest_point_can_be_used_with_slots(self):
        point = LatestPoint()
        point.update(12, 34)
        self.assertEqual(
            point.wait(-1, 0),
            (12, 34, 1, (0, 0, 0, 0, 0, "")),
        )

    def test_identical_point_does_not_generate_work(self):
        point = LatestPoint()
        point.update(12, 34)
        point.update(12, 34)
        self.assertEqual(
            point.wait(-1, 0),
            (12, 34, 1, (0, 0, 0, 0, 0, "")),
        )


if __name__ == "__main__":
    unittest.main()
