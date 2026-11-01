import pathlib
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import check_tls_dependencies


def setup_mock_repo(root: pathlib.Path) -> None:
    tracked_files = [
        "uv.lock",
        "pyproject.toml",
        "scripts/uv_runtime.sh",
        ".github/workflows/tests.yml",
        "rust/node-agent/Cargo.lock",
        "rust/node-driver/Cargo.lock",
        "rust/node-plane-cli/Cargo.lock",
    ]
    for rel_path in tracked_files:
        src = REPO_ROOT / rel_path
        dst = root / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


class TlsDependencyPolicyTests(unittest.TestCase):
    def test_current_policy_succeeds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            setup_mock_repo(root)
            errors = check_tls_dependencies.check(root)
            self.assertEqual(errors, [])

    def test_old_certifi_rejects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            setup_mock_repo(root)
            uv_lock = root / "uv.lock"
            text = uv_lock.read_text(encoding="utf-8")
            self.assertIn('name = "certifi"\nversion = "2026.10.19"', text)
            uv_lock.write_text(
                text.replace('name = "certifi"\nversion = "2026.10.19"', 'name = "certifi"\nversion = "2024.07.04"'),
                encoding="utf-8",
            )
            errors = check_tls_dependencies.check(root)
            self.assertIn("certifi must be locked at 2026.10.19 or newer", errors)

    def test_old_uv_rejects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            setup_mock_repo(root)
            uv_lock = root / "uv.lock"
            text = uv_lock.read_text(encoding="utf-8")
            self.assertIn('name = "uv"\nversion = "0.13.6"', text)
            uv_lock.write_text(
                text.replace('name = "uv"\nversion = "0.13.6"', 'name = "uv"\nversion = "0.13.3"'),
                encoding="utf-8",
            )
            errors = check_tls_dependencies.check(root)
            self.assertTrue(any("uv must be locked at 0.13.4 or newer" in e for e in errors))

    def test_duplicate_affected_certifi_entry_rejects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            setup_mock_repo(root)
            uv_lock = root / "uv.lock"
            duplicate_entry = (
                '\n[[package]]\n'
                'name = "certifi"\n'
                'version = "2023.7.22"\n'
                'source = { registry = "https://pypi.org/simple" }\n'
            )
            uv_lock.write_text(uv_lock.read_text(encoding="utf-8") + duplicate_entry, encoding="utf-8")
            errors = check_tls_dependencies.check(root)
            self.assertIn("certifi must be locked at 2026.10.19 or newer", errors)

    def test_cli_old_root_bundles_reject(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            setup_mock_repo(root)
            cli_lock = root / "rust" / "node-plane-cli" / "Cargo.lock"
            original_text = cli_lock.read_text(encoding="utf-8")

            # Outdated standalone webpki-roots < 1.0.0
            cli_lock.write_text(
                original_text.replace(
                    'name = "webpki-roots"\nversion = "1.0.10"',
                    'name = "webpki-roots"\nversion = "0.25.0"',
                ),
                encoding="utf-8",
            )
            errors = check_tls_dependencies.check(root)
            self.assertIn("node-plane-cli: unreviewed legacy root bundle", errors)

            # Outdated webpki-roots 1.x (< 1.0.10)
            cli_lock.write_text(
                original_text.replace(
                    'name = "webpki-roots"\nversion = "1.0.10"',
                    'name = "webpki-roots"\nversion = "1.0.9"',
                ),
                encoding="utf-8",
            )
            errors = check_tls_dependencies.check(root)
            self.assertIn("node-plane-cli: outdated webpki-roots root bundle", errors)

            # Outdated webpki-root-certs (< 1.0.10)
            cli_lock.write_text(
                original_text.replace(
                    'name = "webpki-root-certs"\nversion = "1.0.10"',
                    'name = "webpki-root-certs"\nversion = "1.0.8"',
                ),
                encoding="utf-8",
            )
            errors = check_tls_dependencies.check(root)
            self.assertIn("node-plane-cli: outdated webpki-root-certs root bundle", errors)

    def test_each_component_old_rustls_webpki_rejects(self):
        for component in ("node-agent", "node-driver", "node-plane-cli"):
            with self.subTest(component=component):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = pathlib.Path(temp_dir)
                    setup_mock_repo(root)
                    cargo_lock = root / "rust" / component / "Cargo.lock"
                    text = cargo_lock.read_text(encoding="utf-8")

                    # Below minimum approved version (0.103.16)
                    cargo_lock.write_text(
                        text.replace(
                            'name = "rustls-webpki"\nversion = "0.103.16"',
                            'name = "rustls-webpki"\nversion = "0.103.15"',
                        ),
                        encoding="utf-8",
                    )
                    errors = check_tls_dependencies.check(root)
                    self.assertIn(f"{component}: review rustls-webpki 0.103.15", errors)

                    # Above approved minor range (< 0.104.0)
                    cargo_lock.write_text(
                        text.replace(
                            'name = "rustls-webpki"\nversion = "0.103.16"',
                            'name = "rustls-webpki"\nversion = "0.104.0"',
                        ),
                        encoding="utf-8",
                    )
                    errors = check_tls_dependencies.check(root)
                    self.assertIn(f"{component}: review rustls-webpki 0.104.0", errors)

                    # Missing rustls-webpki entirely
                    no_webpki_packages = [
                        pkg for pkg in text.split("[[package]]\n")
                        if 'name = "rustls-webpki"' not in pkg
                    ]
                    cargo_lock.write_text("[[package]]\n".join(no_webpki_packages), encoding="utf-8")
                    errors = check_tls_dependencies.check(root)
                    self.assertIn(f"{component}: rustls-webpki lock entry missing", errors)

    def test_installer_and_ci_uv_pin_drift_rejects(self):
        # Installer script pin drift
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            setup_mock_repo(root)
            uv_sh = root / "scripts" / "uv_runtime.sh"
            uv_sh.write_text(
                uv_sh.read_text(encoding="utf-8").replace("NODE_PLANE_UV_VERSION=0.13.6", "NODE_PLANE_UV_VERSION=0.13.5"),
                encoding="utf-8",
            )
            errors = check_tls_dependencies.check(root)
            self.assertIn("controller installer uv pin differs from the approved version", errors)

        # CI workflow pin drift
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            setup_mock_repo(root)
            ci_yml = root / ".github" / "workflows" / "tests.yml"
            ci_yml.write_text(
                ci_yml.read_text(encoding="utf-8").replace("pip install uv==0.13.6", "pip install uv==0.13.5"),
                encoding="utf-8",
            )
            errors = check_tls_dependencies.check(root)
            self.assertIn("CI uv pins differ from the approved version", errors)

        # pyproject.toml pin drift
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            setup_mock_repo(root)
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace('"uv==0.13.6"', '"uv==0.13.5"'),
                encoding="utf-8",
            )
            errors = check_tls_dependencies.check(root)
            self.assertIn("uv project and lock pins differ from the approved installer version", errors)

    def test_legacy_webpki_roots_compatibility_wrapper_vs_standalone_old_bundle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            setup_mock_repo(root)
            cli_lock = root / "rust" / "node-plane-cli" / "Cargo.lock"
            original_text = cli_lock.read_text(encoding="utf-8")

            # Safe legacy compatibility wrapper: version < 1.0.0 alongside 1.x bundle,
            # depending on the reviewed webpki-roots 1.x bundle.
            safe_wrapper = (
                '\n[[package]]\n'
                'name = "webpki-roots"\n'
                'version = "0.26.0"\n'
                'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
                'checksum = "dummy"\n'
                'dependencies = [\n'
                ' "rustls-pki-types",\n'
                ' "webpki-roots 1.0.10",\n'
                ']\n'
            )
            cli_lock.write_text(original_text + safe_wrapper, encoding="utf-8")
            errors = check_tls_dependencies.check(root)
            self.assertEqual(errors, [])

            # Standalone old bundle: version < 1.0.0 and does NOT depend on webpki-roots 1.x
            standalone_old = (
                '\n[[package]]\n'
                'name = "webpki-roots"\n'
                'version = "0.26.0"\n'
                'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
                'checksum = "dummy"\n'
                'dependencies = [\n'
                ' "rustls-pki-types",\n'
                ']\n'
            )
            cli_lock.write_text(original_text + standalone_old, encoding="utf-8")
            errors = check_tls_dependencies.check(root)
            self.assertIn("node-plane-cli: unreviewed legacy root bundle", errors)


if __name__ == "__main__":
    unittest.main()
