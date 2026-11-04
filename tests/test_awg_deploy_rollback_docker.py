"""An AWG deployment from an older config that fails, through the built
agent. In the Docker suite."""
import os
import pathlib
import tempfile
import unittest

from tests.test_container_deploy_ownership_docker import (
    FakeDocker,
    prepare_deployment_env,
    execute_deploy,
)

@unittest.skipUnless(os.environ.get("NODE_PLANE_TEST_DOCKER") == "1", "requires the Docker suite")
class AwgDeployRollbackTests(unittest.TestCase):
    def test_failed_new_container_restores_old_config_and_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "awg", fake_docker)

            # Preserve the original regression's migration from an older config.
            new_fields = ("HeaderProtectionKey =", "ContentPaddingAddition =",
                          "RandomTrailers =", "DisableCookies =", "RekeyAfterTime =",
                          "RekeyTimeout =", "RejectAfterTime =", "KeepaliveTimeout =",
                          "MaxHandshakeAttempts =")
            original_config = '\n'.join(line for line in dep["config_file"].read_text().splitlines()
                                        if not line.startswith(new_fields)) + '\n'
            dep["config_file"].write_text(original_config)

            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "awg",
            }
            old_id = fake_docker.add_container(
                dep["container_name"],
                dep["mount_source"],
                dep["mount_dest"],
                rw=True,
                labels=old_labels,
            )

            # Trigger failure on new container startup
            fake_docker.set_fail_run_status("exited")

            res = execute_deploy(dep, timeout=15)
            self.assertNotEqual(res.returncode, 0)
            self.assertEqual(dep["config_file"].read_text(encoding="utf-8"), original_config)
            self.assertIn("previous config and container restored", res.stderr)

            # Check that docker calls log exists and logged immutable ID mutations
            self.assertTrue(fake_docker.log_file.exists(), res.stderr)
            calls = fake_docker.log_file.read_text(encoding="utf-8")
            self.assertIn(f"rename {old_id} {dep['container_name']}-previous-", calls)
            self.assertIn(f"rename {old_id} {dep['container_name']}", calls)
            self.assertIn(f"start {old_id}", calls)

            # Validate mutation records
            mutations = fake_docker.get_mutations()
            self.assertTrue(len(mutations) > 0)
            for m in mutations:
                self.assertTrue(m["valid_id"], f"Mutation target was not 64-hex ID: {m}")

            # Verify container restored to original state in fake docker
            c = fake_docker.get_container(old_id)
            self.assertIsNotNone(c)
            self.assertEqual(c["Name"], "/" + dep["container_name"])
            self.assertEqual(c["State"]["Status"], "running")


if __name__ == "__main__":
    unittest.main()
