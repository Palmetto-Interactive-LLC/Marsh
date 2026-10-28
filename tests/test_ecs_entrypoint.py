from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from tests.support import load_module


shim = load_module("marsh_ecs_entrypoint_test", "orchestrator/ecs_entrypoint.py")

TOML = '[github]\norg = "example-org"\nrunner_group = "daytona"\n'
PEM = "-----BEGIN RSA PRIVATE KEY-----\nQUJD\n-----END RSA PRIVATE KEY-----"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class EcsEntrypointTests(unittest.TestCase):
    def test_unset_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": "/bin", "MARSH_SHIM_DIR": tmp + "/x"}
            self.assertEqual(shim.prepare(env), env)
            self.assertFalse(Path(tmp, "x").exists())

    def test_blank_values_are_ignored_but_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {shim.TOML_ENV: "  ", shim.PEM_ENV: "", "MARSH_SHIM_DIR": tmp}
            child = shim.prepare(env)
            self.assertNotIn(shim.TOML_ENV, child)
            self.assertNotIn(shim.PEM_ENV, child)
            self.assertEqual(os.listdir(tmp), [])

    def test_writes_both_files_private_and_sets_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp, "cfg")
            child = shim.prepare({shim.TOML_ENV: TOML, shim.PEM_ENV: PEM, "MARSH_SHIM_DIR": str(base)})
            config, key = Path(child["MARSH_RUNNER_CONFIG"]), Path(child["GH_APP_KEY_PATH"])
            self.assertEqual(config, base / "runners.toml")
            self.assertEqual(key, base / "github-app.pem")
            self.assertEqual(config.read_text(), TOML)
            self.assertEqual(key.read_text(), PEM + "\n")
            self.assertEqual(mode(config), 0o600)
            self.assertEqual(mode(key), 0o600)
            self.assertEqual(mode(base), 0o700)
            self.assertEqual(sorted(os.listdir(base)), ["github-app.pem", "runners.toml"])

    def test_source_variables_are_removed_from_child_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            child = shim.prepare({shim.TOML_ENV: TOML, shim.PEM_ENV: PEM, "MARSH_SHIM_DIR": tmp})
            self.assertNotIn(shim.TOML_ENV, child)
            self.assertNotIn(shim.PEM_ENV, child)

    def test_explicit_target_paths_are_honored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp, "etc", "runners.toml")
            key = Path(tmp, "run", "k.pem")
            child = shim.prepare({
                shim.TOML_ENV: TOML,
                shim.PEM_ENV: PEM,
                "MARSH_RUNNER_CONFIG": str(config),
                "GH_APP_KEY_PATH": str(key),
            })
            self.assertEqual(child["MARSH_RUNNER_CONFIG"], str(config))
            self.assertEqual(child["GH_APP_KEY_PATH"], str(key))
            self.assertTrue(config.is_file())
            self.assertTrue(key.is_file())

    def test_only_toml_leaves_key_path_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            child = shim.prepare({shim.TOML_ENV: TOML, "MARSH_SHIM_DIR": tmp, "GH_APP_KEY_PATH": "/etc/marsh/github-app.pem"})
            self.assertEqual(child["GH_APP_KEY_PATH"], "/etc/marsh/github-app.pem")
            self.assertFalse(Path(tmp, "github-app.pem").exists())

    def test_literal_backslash_n_pem_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flat = PEM.replace("\n", "\\n")
            child = shim.prepare({shim.PEM_ENV: flat, "MARSH_SHIM_DIR": tmp})
            self.assertEqual(Path(child["GH_APP_KEY_PATH"]).read_text(), PEM + "\n")

    def test_rejects_non_pem_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(shim.ShimError) as ctx:
                shim.prepare({shim.PEM_ENV: "hunter2-not-a-key", "MARSH_SHIM_DIR": tmp})
            self.assertNotIn("hunter2", str(ctx.exception))
            self.assertEqual(os.listdir(tmp), [])

    def test_rejects_invalid_toml_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(shim.ShimError) as ctx:
                shim.prepare({shim.TOML_ENV: "token = hunter2 [[", "MARSH_SHIM_DIR": tmp})
            self.assertNotIn("hunter2", str(ctx.exception))

    def test_rewrite_replaces_existing_file_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "runners.toml")
            target.write_text("old\n")
            os.chmod(target, 0o644)
            shim.prepare({shim.TOML_ENV: TOML, "MARSH_SHIM_DIR": tmp})
            self.assertEqual(target.read_text(), TOML)
            self.assertEqual(mode(target), 0o600)

    def test_default_command_matches_image_entrypoint(self) -> None:
        self.assertEqual(shim.DEFAULT_COMMAND, ["python", "/app/orchestrator.py"])


if __name__ == "__main__":
    unittest.main()
