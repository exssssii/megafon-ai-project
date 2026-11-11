from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import image_bytes  # noqa: E402
import image_header  # noqa: E402


class Truncation(unittest.TestCase):
    def test_complete_files_are_read(self):
        for data, expected in ((image_bytes.png_bytes(30, 20), "png"), (image_bytes.jpeg_header(30, 20), "jpeg"),
                               (image_bytes.webp_vp8x(30, 20), "webp")):
            with self.subTest(expected):
                self.assertEqual(image_header.read_header(data).format, expected)

    def test_png_without_end_chunk_is_rejected(self):
        data = image_bytes.png_bytes(30, 20)
        for cut in (12, 40, 100):
            with self.subTest(cut):
                with self.assertRaises(image_header.NotImage):
                    image_header.read_header(data[:-cut])

    def test_jpeg_without_end_marker_is_rejected(self):
        data = image_bytes.jpeg_header(30, 20)
        with self.assertRaises(image_header.NotImage):
            image_header.read_header(data[:-2])

    def test_small_trailing_padding_is_tolerated(self):
        self.assertEqual(image_header.read_header(image_bytes.png_bytes(30, 20) + b"\x00" * 10).width, 30)
        self.assertEqual(image_header.read_header(image_bytes.jpeg_header(30, 20) + b"\x00" * 10).width, 30)

    def test_long_trailing_data_hides_the_end(self):
        with self.assertRaises(image_header.NotImage):
            image_header.read_header(image_bytes.png_bytes(30, 20) + b"\x00" * 200)
        with self.assertRaises(image_header.NotImage):
            image_header.read_header(image_bytes.jpeg_header(30, 20) + b"\x00" * 200)


class Animation(unittest.TestCase):
    def test_animated_png_is_rejected_with_a_reason(self):
        with self.assertRaises(image_header.NotImage) as caught:
            image_header.read_header(image_bytes.png_animated(30, 20))
        self.assertEqual(str(caught.exception), "анимация не поддерживается")
        self.assertIsInstance(caught.exception, image_header.Animated)

    def test_animated_webp_is_rejected_with_a_reason(self):
        with self.assertRaises(image_header.Animated):
            image_header.read_header(image_bytes.webp_vp8x(30, 20, flags=0x02))

    def test_webp_with_other_flags_is_read(self):
        self.assertEqual(image_header.read_header(image_bytes.webp_vp8x(30, 20, flags=0x30)).height, 20)

    def test_control_chunk_after_image_data_is_not_animation(self):
        plain = image_bytes.png_bytes(30, 20)
        split = len(plain) - 12
        late = plain[:split] + image_bytes._chunk(b"acTL", b"\x00" * 8) + plain[split:]
        self.assertEqual(image_header.read_header(late).format, "png")


if __name__ == "__main__":
    unittest.main()
