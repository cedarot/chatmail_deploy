import contextlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import notification_gateway as gateway
import notification_metadata as metadata


def result(stdout="", stderr="", code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


def configuration(root):
    return {"container": "mail-test", "gateway": "gateway-test", "python": "/venv/bin/python",
            "original_argv": ["/venv/bin/python", "/venv/bin/chatmail-metadata", "/run/metadata.sock", "/config.ini"],
            "original_unit": "# /etc/systemd/system/chatmail-metadata.service\n[Service]\nUser=vmail\n",
            "image_id": "image-before", "domain": "chat.example.org", "queue": "/mail/pending_notifications",
            "queue_dir": str(root / "queue"), "adapter": "/opt/notify/metadata.py",
            "dropin": "/etc/systemd/system/chatmail-metadata.service.d/90-notification-test.conf",
            "root": str(root), "unit": "chatmail-notification-test", "port": 10089, "ca": "/ca.pem", "sni": True,
            "enabled": True, "phase": "enabled"}


class TransportTests(unittest.TestCase):
    def test_probe_requires_chain_hostname_and_405_without_sni(self):
        with patch.object(gateway, "run", return_value=result("HTTP/1.1 405 Method Not Allowed\r\n", "Verification: OK\n", 1)) as run:
            self.assertTrue(gateway.probe("1.1.1.1", False, "/ca.pem")["certificate_verified"])
            args = run.call_args.args[0]
            self.assertIn("-noservername", args)
            self.assertIn("-verify_return_error", args)
            self.assertEqual(args[args.index("-verify_hostname") + 1], gateway.HOST)
            self.assertEqual(args[args.index("-CAfile") + 1], "/ca.pem")

    def test_rejects_invalid_certificate_or_wrong_http_status(self):
        for response, verification in [("HTTP/1.1 405 OK\r\n", "Verification error: hostname mismatch"),
                                       ("HTTP/1.1 200 OK\r\n", "Verification: OK")]:
            with self.subTest(response=response), patch.object(gateway, "run", return_value=result(response, verification, 1)):
                with self.assertRaises(gateway.ProbeError):
                    gateway.probe("1.1.1.1", True, "/ca.pem")

    def test_sni_is_on_by_default(self):
        with patch.object(gateway, "verified_addresses", return_value=[{"ip": "1.1.1.1"}]), patch.object(gateway, "probe") as probe:
            self.assertTrue(gateway.sni_policy("/ca.pem")[0])
            probe.assert_not_called()

    def test_workaround_requires_repeated_same_ip_reset(self):
        def probe(ip, sni, ca):
            if sni:
                raise gateway.ProbeError("reset")
            return {"ip": ip, "status": 405}

        with patch.object(gateway, "verified_addresses", side_effect=gateway.GatewayError), \
             patch.object(gateway, "addresses", return_value=iter([["1.1.1.1"]])), patch.object(gateway, "probe", side_effect=probe) as request:
            sni, checks, evidence = gateway.sni_policy("/ca.pem", True)
        self.assertFalse(sni)
        self.assertEqual(request.call_count, 6)
        self.assertEqual(checks[0]["ip"], "1.1.1.1")
        self.assertEqual(len(evidence), 3)

    def test_timeout_or_http_failure_is_not_sni_reset_proof(self):
        with patch.object(gateway, "verified_addresses", side_effect=gateway.GatewayError), \
             patch.object(gateway, "addresses", return_value=iter([["1.1.1.1"]])), \
             patch.object(gateway, "probe", side_effect=gateway.ProbeError("timeout")):
            with self.assertRaises(gateway.GatewayError):
                gateway.sni_policy("/ca.pem", True)

    def test_dns_filters_private_answers_and_handles_resolver_failure(self):
        answer = {"Status": 0, "Answer": [{"type": 1, "data": "127.0.0.1"}, {"type": 1, "data": "1.1.1.1"}]}
        with patch.object(gateway, "DOH", ["https://resolver-a", "https://resolver-b"]), \
             patch.object(gateway, "run", side_effect=[gateway.GatewayError(), result(json.dumps(answer))]):
            self.assertEqual(list(gateway.addresses()), [["1.1.1.1"]])


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "nginx").mkdir()
        self.path = self.root / "nginx/upstream.inc"
        self.path.write_text("previous valid configuration\n")
        self.config = configuration(self.root)

    def test_existing_nginx_directory_permissions_are_repaired_without_exposing_state(self):
        self.root.chmod(0o700)
        (self.root / "nginx").chmod(0o750)
        previous_umask = os.umask(0o027)
        try:
            nginx_dir = gateway.prepare_nginx_directory(self.root)
        finally:
            os.umask(previous_umask)
        self.assertEqual(nginx_dir.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path.read_text(), "previous valid configuration\n")

    def test_dns_failure_preserves_previous_file(self):
        with patch.object(gateway, "verified_addresses", side_effect=gateway.GatewayError):
            with self.assertRaises(gateway.GatewayError):
                gateway.refresh(self.root, self.config)
        self.assertEqual(self.path.read_text(), "previous valid configuration\n")

    def test_validation_reload_and_http_failures_roll_back(self):
        for step in ("validation", "reload", "http"):
            with self.subTest(step=step):
                effects = {"validation": [gateway.GatewayError(), None, None],
                           "reload": [None, gateway.GatewayError(), None, None], "http": [None] * 4}
                with patch.object(gateway, "verified_addresses", return_value=[{"ip": "1.1.1.1"}]), \
                     patch.object(gateway, "nginx", side_effect=effects[step]) as nginx, \
                     patch.object(gateway, "gateway_get", return_value=False):
                    with self.assertRaises(gateway.GatewayError):
                        gateway.refresh(self.root, self.config)
                self.assertEqual(self.path.read_text(), "previous valid configuration\n")
                self.assertEqual(nginx.call_args.args[1:], ("-s", "reload"))
                self.assertFalse((self.root / "refresh.pending").exists())

    def test_interrupted_refresh_restores_before_network_resolution(self):
        gateway.save(self.root / "refresh.pending", {"previous": "last working configuration\n"})
        self.path.write_text("interrupted candidate\n")
        with patch.object(gateway, "nginx") as nginx, patch.object(gateway, "verified_addresses", side_effect=gateway.GatewayError):
            with self.assertRaises(gateway.GatewayError):
                gateway.refresh(self.root, self.config)
        self.assertEqual(self.path.read_text(), "last working configuration\n")
        self.assertEqual(nginx.call_count, 2)

    def test_unchanged_addresses_do_not_reload(self):
        checks = [{"ip": "1.1.1.1"}]
        self.path.write_text(gateway.upstream(checks))
        with patch.object(gateway, "verified_addresses", return_value=checks), patch.object(gateway, "nginx") as nginx:
            gateway.refresh(self.root, self.config)
        self.assertEqual(nginx.call_args.args[1:], ("-t",))
        self.assertEqual(nginx.call_count, 1)

    def test_gateway_container_is_hardened_and_directory_mounted(self):
        config = gateway.gateway_compose(self.root, self.config, "nginx@sha256:test")
        service = config["services"]["gateway"]
        self.assertTrue(service["read_only"])
        self.assertEqual(service["user"], "101:101")
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertEqual(service["volumes"][0]["source"], str(self.root / "nginx"))
        self.assertEqual(service["restart"], "unless-stopped")
        template = (gateway.HERE / "config/notification-nginx.conf").read_text()
        self.assertIn("listen 127.0.0.1:@@PORT@@", template)
        self.assertIn("proxy_ssl_verify on", template)
        self.assertIn("proxy_ssl_name notifications.delta.chat", template)
        self.assertNotIn("$request_body", template)

    def test_dropin_preserves_installed_python_socket_and_configuration(self):
        content = gateway.dropin(self.config)
        self.assertIn('ExecStart="/venv/bin/python" "/opt/notify/metadata.py" "/run/metadata.sock" "/config.ini"', content)
        self.assertIn("CHATMAIL_NOTIFY_URL=http://127.0.0.1:10089/notify", content)
        self.assertIn("ExecStart=\n", content)

    def test_systemd_paths_escape_specifiers_and_env_without_shell_execution(self):
        self.assertEqual(gateway.systemd_quote('/srv/relay space%$"'), '"/srv/relay space%%$$\\""')
        self.assertIn("$", gateway.systemd_quote("/srv/$relay", environment=False))
        with self.assertRaises(gateway.GatewayError):
            gateway.systemd_quote("/srv/relay\nExecStart=bad")

    def test_queue_must_map_to_longest_writable_persistent_mount(self):
        mounts = [{"Type": "bind", "Destination": "/mail", "Source": "/data", "RW": True},
                  {"Type": "volume", "Destination": "/mail/domain", "Source": "/volume", "RW": True}]
        self.assertEqual(gateway.map_queue("/mail/domain/pending_notifications", mounts), "/volume/pending_notifications")
        with self.assertRaises(gateway.GatewayError):
            gateway.map_queue("/other/pending_notifications", mounts)
        with self.assertRaises(gateway.GatewayError):
            gateway.map_queue("/mail/pending_notifications", [dict(mounts[0], RW=False)])

    def test_queue_stats_ignore_temporary_files_and_never_read_tokens(self):
        queue = self.root / "queue"
        queue.mkdir()
        (queue / "task").write_text("not a real token")
        (queue / "task.tmp").touch()
        with patch.object(Path, "read_text", side_effect=AssertionError("body read")):
            self.assertEqual(gateway.queue_stats(self.config)["count"], 1)
        self.assertTrue((queue / "task").exists())

    def test_apply_rejects_image_or_unit_changes_before_writing(self):
        with patch.object(gateway, "inspect", return_value={"Image": "new-image"}), patch.object(gateway, "docker_python") as execute:
            with self.assertRaises(gateway.GatewayError):
                gateway.apply_adapter(self.root, self.config)
            execute.assert_not_called()
        with patch.object(gateway, "check_image"), patch.object(gateway, "docker_python", return_value=result(json.dumps({k: self.config[k] for k in ("domain", "queue")}))) as execute, \
             patch.object(gateway, "run", return_value=result("user changed unit")):
            with self.assertRaises(gateway.GatewayError):
                gateway.apply_adapter(self.root, self.config)
            self.assertEqual(execute.call_count, 1)  # introspection only

    def test_apply_preserves_other_dropins(self):
        unit = self.config["original_unit"] + "\n# " + self.config["dropin"] + "\n" + gateway.dropin(self.config)
        (self.root / "notification_metadata.py").write_text("# adapter")
        with patch.object(gateway, "check_image"), patch.object(gateway, "docker_python", return_value=result(json.dumps({k: self.config[k] for k in ("domain", "queue")}))), \
             patch.object(gateway, "run", return_value=result(unit)) as run:
            gateway.apply_adapter(self.root, self.config)
        self.assertIn(["restart", gateway.SERVICE], [call.args[0][-2:] for call in run.call_args_list])

    def test_runtime_adapter_remains_readable_under_restrictive_umask(self):
        target = self.root / "new-runtime/instance/adapter.py"
        previous = os.umask(0o077)
        try:
            with patch.object(sys, "argv", ["write-runtime", str(target)]), patch.object(sys, "stdin", io.StringIO("# adapter")):
                exec(gateway.WRITE_RUNTIME, {})
        finally:
            os.umask(previous)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(target.parent.parent.stat().st_mode & 0o777, 0o755)


class PrivacyTests(unittest.TestCase):
    def test_formatter_never_exposes_tokens_bodies_passwords_or_tracebacks(self):
        formatter = metadata.PrivateFormatter()
        secret = "FAKE_PRIVATE_SENTINEL"
        for message in ("notification exceeded deadline: " + secret, "unknown dictproxy request: " + secret,
                        "Notification request failed: " + secret, "unrecognized " + secret):
            record = logging.LogRecord("root", logging.ERROR, "module.py", 3, message, (), (ValueError, ValueError(secret), None), "handler")
            self.assertNotIn(secret, formatter.format(record))
        malformed = logging.LogRecord("root", logging.ERROR, "module.py", 3, "%s %s", (secret,), None)
        self.assertNotIn(secret, formatter.format(malformed))

    def test_adapter_uses_installed_main_and_only_overrides_url(self):
        notifier = SimpleNamespace(Notifier=type("Notifier", (), {"URL": "original"}))
        installed_main = Mock(return_value=7)
        package = SimpleNamespace(metadata=SimpleNamespace(main=installed_main), notifier=notifier)
        with patch.dict(sys.modules, {"chatmaild": package}), \
             patch.dict("os.environ", {"CHATMAIL_NOTIFY_URL": "http://127.0.0.1:10089/notify"}), \
             patch.object(logging, "basicConfig"), patch.object(logging, "getLogger", return_value=SimpleNamespace(handlers=[])):
            self.assertEqual(metadata.main(), 7)
        installed_main.assert_called_once_with()
        self.assertEqual(notifier.Notifier.URL, "http://127.0.0.1:10089/notify")


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = configuration(self.root)
        (self.root / "queue").mkdir()
        self.healthy = {"State": {"Running": True, "StartedAt": "2020-01-01T00:00:00Z", "Health": {"Status": "healthy"}}}

    def test_three_failure_threshold_and_ten_minute_cooldown(self):
        state = {}
        self.assertFalse(gateway.failed(state, "metadata", False))
        self.assertFalse(gateway.failed(state, "metadata", False))
        self.assertTrue(gateway.failed(state, "metadata", False))
        self.assertFalse(gateway.failed(state, "metadata", True))
        self.assertFalse(gateway.cooled({"restart": 1000}, "restart", 1599))
        self.assertTrue(gateway.cooled({"restart": 1000}, "restart", 1600))
        self.assertFalse(gateway.cooled({}, "restart", 1600, {"State": {}}))

    def test_network_outage_and_old_queue_do_not_restart_mail(self):
        for i in range(3):
            with patch.object(gateway, "inspect", return_value=self.healthy), \
                 patch.object(gateway, "gateway_get", side_effect=lambda config, path="/notify", expected=405: path == "/healthz"), \
                 patch.object(gateway, "adapter_present", return_value=True), \
                 patch.object(gateway, "queue_stats", return_value={"count": 4, "oldest_age_seconds": 900}), \
                 patch.object(gateway, "run", return_value=result("active\nactive\nactive\n")) as run, \
                 patch.object(gateway, "refresh") as refresh:
                self.assertFalse(gateway.health(self.root, self.config))
                for call in run.call_args_list:
                    self.assertNotIn("restart", call.args[0])
                if i == 2:
                    refresh.assert_called_once_with(self.root, self.config)

    def test_metadata_recovery_only_after_three_failures_and_is_cooled(self):
        with patch.object(gateway, "inspect", return_value=self.healthy), \
             patch.object(gateway, "gateway_get", return_value=True), \
             patch.object(gateway, "run", return_value=result("active\nactive\ninactive\n", code=3)), \
             patch.object(gateway, "apply_adapter") as apply:
            for _ in range(2):
                gateway.health(self.root, self.config)
            apply.assert_not_called()
            gateway.health(self.root, self.config)
            self.assertEqual(apply.call_count, 1)
            gateway.health(self.root, self.config)
            self.assertEqual(apply.call_count, 1)

    def test_missing_containers_and_command_timeouts_are_recorded(self):
        with patch.object(gateway, "inspect", side_effect=gateway.GatewayError), \
             patch.object(gateway, "run", side_effect=gateway.GatewayError):
            gateway.health(self.root, self.config)
        state = gateway.read_json(self.root / "health.json")
        self.assertFalse(state["mail_ok"])
        self.assertFalse(state["metadata_ok"])
        self.assertEqual(state["metadata_failures"], 1)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "notification-gateway"
        self.base.mkdir(mode=0o700)
        (self.root / "queue").mkdir()
        self.config = configuration(self.root)
        self.config["compose_files"] = []
        self.args = SimpleNamespace(service=None, port=10089, gateway_image="nginx:test", allow_sni_workaround=False)
        self.units = self.root / "units"
        self.units.mkdir()

    def unit_paths(self, config):
        return [self.units / (config["unit"] + suffix) for suffix in
                ("-apply.service", "-health.service", "-health.timer", "-refresh.service", "-refresh.timer")]

    def command(self, args, **kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            return result(json.dumps([{"Id": "image-gateway", "RepoDigests": ["nginx@sha256:test"]}]))
        if args[:2] == ["docker", "exec"] and "test" in args:
            return result(code=1)
        if args[:2] == ["docker", "exec"] and "cat" in args and gateway.SERVICE in args:
            return result(self.config["original_unit"])
        return result()

    def test_enable_and_disable_preserve_mail_and_native_entrypoint(self):
        writes = []
        previous_umask = os.umask(0o027)
        self.addCleanup(os.umask, previous_umask)

        def execute(config, code, *args, data=None):
            if code == gateway.DISCOVER:
                return result(json.dumps({key: config[key] for key in ("domain", "queue")}))
            writes.append((args[0], data))
            return result()

        with patch.object(gateway, "discover", return_value=self.config), patch.object(gateway.socket, "socket"), \
             patch.object(gateway, "sni_policy", return_value=(True, [{"ip": "1.1.1.1"}], [])), \
             patch.object(gateway, "ca_bundle", return_value="/ca.pem"), patch.object(gateway, "unit_paths", side_effect=self.unit_paths), \
             patch.object(gateway, "check_image"), patch.object(gateway, "gateway_get", return_value=True), \
             patch.object(gateway, "docker_python", side_effect=execute), patch.object(gateway, "run", side_effect=self.command) as run:
            gateway.enable(self.root, self.base, self.args)
            self.assertEqual(self.base.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.base / "nginx").stat().st_mode & 0o777, 0o755)
            for name in ("nginx.conf", "upstream.inc"):
                self.assertEqual((self.base / "nginx" / name).stat().st_mode & 0o777, 0o644)
            self.assertEqual((self.base / "config.json").stat().st_mode & 0o777, 0o600)
            config = gateway.read_json(self.base / "config.json")
            self.assertTrue(config["enabled"])
            self.assertEqual(config["phase"], "enabled")
            self.assertTrue(Path(config["backup"]).is_dir())
            self.assertIn("metadata.main()", writes[0][1])
            self.assertIn(config["adapter"], writes[1][1])
            for call in run.call_args_list:
                args = call.args[0]
                if args[:2] == ["docker", "compose"]:
                    self.assertNotIn("--force-recreate", args)
                    if "up" in args:
                        self.assertEqual(args[-1], "gateway")
            with patch.object(gateway, "restore_adapter") as restore:
                gateway.disable(self.base, config)
                restore.assert_called_once_with(config)
            self.assertEqual(list(self.units.iterdir()), [])
            self.assertTrue((self.root / "queue").is_dir())
            self.assertFalse(gateway.read_json(self.base / "config.json")["enabled"])

    def test_partial_apply_failure_restores_entrypoint_and_stops_gateway(self):
        with patch.object(gateway, "discover", return_value=self.config), patch.object(gateway.socket, "socket"), \
             patch.object(gateway, "sni_policy", return_value=(True, [{"ip": "1.1.1.1"}], [])), \
             patch.object(gateway, "ca_bundle", return_value="/ca.pem"), patch.object(gateway, "unit_paths", side_effect=self.unit_paths), \
             patch.object(gateway, "gateway_get", return_value=True), patch.object(gateway, "run", side_effect=self.command), \
             patch.object(gateway, "apply_adapter", side_effect=gateway.GatewayError("restart failed")), \
             patch.object(gateway, "restore_adapter") as restore:
            with self.assertRaises(gateway.GatewayError):
                gateway.enable(self.root, self.base, self.args)
            restore.assert_called_once()
        config = gateway.read_json(self.base / "config.json")
        self.assertFalse(config["enabled"])
        self.assertEqual(config["phase"], "disabled")

    def test_unknown_existing_gateway_files_are_not_overwritten(self):
        (self.base / "notification_gateway.py").write_text("user file")
        with self.assertRaises(gateway.GatewayError):
            gateway.enable(self.root, self.base, self.args)
        self.assertEqual((self.base / "notification_gateway.py").read_text(), "user file")


class NativeConfigurationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd verifier is unavailable")
    def test_generated_units_parse_with_space_dollar_and_percent_in_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "relay space$%"
            base = root / "notification-gateway"
            base.mkdir(parents=True)
            (base / "notification_gateway.py").write_text("# config test only\n")
            config = configuration(root)
            unit_dir = root / "units"
            unit_dir.mkdir()
            paths = [unit_dir / path.name for path in gateway.unit_paths(config)]

            def command(args, **kwargs):
                if args[0] == "systemd-analyze":
                    checked = subprocess.run(args, capture_output=True, text=True)
                    # Some minimal CI images have no docker.service installed.
                    if checked.returncode and "docker.service not found" in checked.stderr:
                        self.skipTest("Docker systemd unit is unavailable in this test environment")
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                return result()

            with patch.object(gateway, "unit_paths", return_value=paths), patch.object(gateway, "run", side_effect=command):
                gateway.install_units(base, config)

    @unittest.skipUnless(shutil.which("nginx"), "local nginx binary is unavailable")
    def test_rendered_nginx_configuration_with_both_sni_policies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca = gateway.ca_bundle()
            (root / "upstream.inc").write_text("server 1.1.1.1:443;\n")
            template = (gateway.HERE / "config/notification-nginx.conf").read_text()
            template = template.replace("/tmp/", str(root) + "/")
            template = template.replace("/etc/chatmail-notify/upstream.inc", str(root / "upstream.inc"))
            template = template.replace("/etc/ssl/certs/ca-certificates.crt", ca)
            for sni in ("on", "off"):
                path = root / "nginx.conf"
                path.write_text(template.replace("@@PORT@@", "10089").replace("@@SNI@@", sni))
                result = subprocess.run(["nginx", "-t", "-c", str(path), "-p", directory, "-e", "stderr"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI is unavailable")
    def test_generated_compose_validates_without_daemon(self):
        version = subprocess.run(["docker", "compose", "version"], capture_output=True)
        if version.returncode:
            self.skipTest("Docker Compose plugin is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = configuration(root)
            config["ca"] = gateway.ca_bundle()
            path = root / "compose.json"
            path.write_text(json.dumps(gateway.gateway_compose(root, config, "nginx:stable-alpine")))
            result = subprocess.run(["docker", "compose", "-f", str(path), "config", "--quiet"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main()
