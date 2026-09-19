import tempfile
import unittest
from pathlib import Path

from device_identity import generate_device_name, load_or_create_device_name


class DeviceIdentityTest(unittest.TestCase):
    def test_generated_name_contains_two_readable_words(self):
        parts = generate_device_name().split("-")
        self.assertEqual(len(parts), 2)

    def test_name_is_generated_once_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "device-name"
            first = load_or_create_device_name(str(path), lambda: "amber-otter")
            second = load_or_create_device_name(str(path), lambda: "silver-fox")
            self.assertEqual(first, "amber-otter")
            self.assertEqual(second, first)
            self.assertEqual(path.read_text().strip(), first)


if __name__ == "__main__":
    unittest.main()
