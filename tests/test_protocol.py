import time
import unittest

from contextscroll.classifier import Decision
from contextscroll.context_agent import LatestPoint
from contextscroll.protocol import (
    ActivityReport,
    ContextRegistry,
    ContextReport,
    CursorReport,
    MAX_LINE_BYTES,
    RefreshReport,
    decode,
    decode_activity,
    decode_daemon,
    encode,
    encode_control,
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
            decode_activity(
                b'{"v":2,"type":"activity","active":true,'
                b'"generation":9}\n'
            ),
            ActivityReport(True, 9),
        )

    def test_invalid_activity_report_is_rejected(self):
        with self.assertRaises(ValueError):
            decode_activity(
                b'{"v":2,"type":"activity","active":"yes"}\n'
            )

    def test_boolean_activity_generation_is_rejected(self):
        with self.assertRaises(ValueError):
            decode_activity(
                b'{"v":2,"type":"activity","active":true,'
                b'"generation":true}\n'
            )

    def test_refresh_report_round_trip_from_daemon(self):
        self.assertEqual(
            decode_daemon(
                b'{"v":2,"type":"refresh","request_id":17}\n'
            ),
            RefreshReport(17),
        )

    def test_cursor_report_round_trip_from_daemon(self):
        self.assertEqual(
            decode_daemon(
                b'{"v":2,"type":"cursor","x":-12,"y":34,'
                b'"direction":5}\n'
            ),
            CursorReport(-12, 34, 5),
        )

    def test_cursor_report_rejects_boolean_coordinates(self):
        with self.assertRaises(ValueError):
            decode_daemon(
                b'{"v":2,"type":"cursor","x":true,"y":34,'
                b'"direction":1}\n'
            )

    def test_cursor_report_rejects_invalid_direction(self):
        with self.assertRaises(ValueError):
            decode_daemon(
                b'{"v":2,"type":"cursor","x":12,"y":34,'
                b'"direction":2}\n'
            )

    def test_pause_control_encoding_is_bounded(self):
        self.assertEqual(
            encode_control(True),
            b'{"v":2,"type":"control","paused":true}\n',
        )
        with self.assertRaises(ValueError):
            encode_control(1)

    def test_context_report_carries_refresh_acknowledgement(self):
        report = ContextReport(
            Decision.SCROLL,
            x=20,
            y=30,
            request_id=17,
            generation=3,
        )
        self.assertEqual(decode(encode(report)), report)

    def test_latest_point_can_be_used_with_slots(self):
        point = LatestPoint()
        point.update(12, 34)
        self.assertEqual(
            point.wait(-1, 0),
            (12, 34, 1, None),
        )

    def test_identical_point_does_not_generate_work(self):
        point = LatestPoint()
        point.update(12, 34)
        point.update(12, 34)
        self.assertEqual(
            point.wait(-1, 0),
            (12, 34, 1, None),
        )

    def test_explicit_refresh_generates_work_at_same_point(self):
        point = LatestPoint()
        point.update(12, 34)
        point.refresh()
        self.assertEqual(
            point.wait(-1, 0),
            (12, 34, 2, None),
        )


if __name__ == "__main__":
    unittest.main()
