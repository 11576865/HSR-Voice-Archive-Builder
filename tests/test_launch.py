from __future__ import annotations

import unittest
from unittest.mock import patch

from app.launch import full_server_available


class LaunchFallbackTests(unittest.TestCase):
    def test_missing_uvicorn_selects_lite_capability(self) -> None:
        def fake_find_spec(name: str):
            return None if name == "uvicorn" else object()

        with patch("app.launch.importlib.util.find_spec", side_effect=fake_find_spec):
            self.assertFalse(full_server_available())

    def test_complete_stack_is_available(self) -> None:
        with patch("app.launch.importlib.util.find_spec", return_value=object()):
            self.assertTrue(full_server_available())


if __name__ == "__main__":
    unittest.main()
