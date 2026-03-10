from __future__ import annotations

import unittest

from app.utils.security import (
    shell_env_assignment,
    validate_profile_name,
    validate_server_field,
    validate_server_key,
)


class SecurityValidationTests(unittest.TestCase):
    def test_validate_profile_name_accepts_safe_value(self) -> None:
        self.assertEqual(validate_profile_name("@user.name-1"), "user.name-1")

    def test_validate_profile_name_rejects_quotes(self) -> None:
        with self.assertRaises(ValueError):
            validate_profile_name('bad"name')

    def test_validate_server_key_rejects_shell_chars(self) -> None:
        with self.assertRaises(ValueError):
            validate_server_key("de;rm")

    def test_validate_server_field_rejects_unsafe_iface(self) -> None:
        with self.assertRaises(ValueError):
            validate_server_field("awg_iface", "wg0; id")

    def test_validate_server_field_rejects_unsafe_path(self) -> None:
        with self.assertRaises(ValueError):
            validate_server_field("xray_config_path", "/tmp/xray config.json")

    def test_validate_server_field_rejects_newline_in_notes(self) -> None:
        with self.assertRaises(ValueError):
            validate_server_field("notes", "hello\nworld")

    def test_shell_env_assignment_quotes_payload(self) -> None:
        rendered = shell_env_assignment("AWG_IFACE", "$(touch /tmp/pwned)")
        self.assertEqual(rendered, "AWG_IFACE='$(touch /tmp/pwned)'")


if __name__ == "__main__":
    unittest.main()
