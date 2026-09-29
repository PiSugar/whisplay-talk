import importlib.util
import os
import unittest


SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "tools",
    "espnow_channel_control.py",
)
SPEC = importlib.util.spec_from_file_location("espnow_channel_control", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class EspNowChannelControlTest(unittest.TestCase):
    def test_parses_channel_mode(self):
        self.assertEqual(module.parse_status(b"S\x06A"), (6, "AUTO"))
        self.assertEqual(module.parse_status(b"S\x0dF"), (13, "FIXED"))
        self.assertEqual(module.parse_status(b"S\x07S"), (7, "SWITCHING"))
        self.assertEqual(module.parse_status(b"S\x02"), (2, "AUTO"))
        self.assertIsNone(module.parse_status(b"S\x00F"))
        self.assertIsNone(module.parse_status(b"S\x06?"))

    def test_next_channel_wraps(self):
        self.assertEqual(module.next_channel(1), 2)
        self.assertEqual(module.next_channel(13), 1)


if __name__ == "__main__":
    unittest.main()
