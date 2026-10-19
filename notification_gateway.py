#!/usr/bin/env python3
"""Opt-in Chatmail notification gateway. Python 3.9+, Linux, Docker and systemd."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time

HOST = "notifications.delta.chat"
DOH = (
    f"https://doh.pub/dns-query?name={HOST}&type=A",
    f"https://cloudflare-dns.com/dns-query?name={HOST}&type=A",
    f"https://dns.google/resolve?name={HOST}&type=A",
)
SERVICE = "chatmail-metadata.service"
HERE = Path(__file__).resolve().parent


class GatewayError(RuntimeError):
    pass


class MetadataNotReady(GatewayError):
    pass


class ProbeError(GatewayError):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def run(args, *, data=None, timeout=30, check=True):
    try:
        result = subprocess.run(args, input=data, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GatewayError(f"{args[0]} command timed out") from None
    if check and result.returncode:
        # Captured command output can contain configuration or credentials.
        raise GatewayError(f"{args[0]} command failed (exit {result.returncode})")
    return result


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def atomic(path, text, mode=0o600):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    atomic(path, json.dumps(value, indent=2) + "\n")


def ca_bundle():
    for candidate in (ssl.get_default_verify_paths().cafile,
                      "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise GatewayError("system CA bundle not found")


def probe(ip, sni, ca):
    """OpenSSL verifies both DNS hostname and CA chain, even without SNI."""
    begin = time.monotonic()
    args = ["openssl", "s_client", "-brief", "-connect", f"{ip}:443",
            "-verify_hostname", HOST, "-verify_return_error", "-CAfile", ca, "-ign_eof"]
    args += ["-servername", HOST] if sni else ["-noservername"]
    request = f"GET /notify HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n"
    try:
        result = run(args, data=request, timeout=10, check=False)
    except GatewayError:
        raise ProbeError("timeout") from None
    # An EOF without close_notify may yield exit 1 after a verified HTTP response.
    verified = "Verification: OK" in result.stderr
    status = re.search(r"^HTTP/1\.[01] 405(?: |\r?$)", result.stdout, re.MULTILINE)
    if verified and status and result.returncode in (0, 1):
        return {"ip": ip, "status": 405, "certificate_verified": True,
                "seconds": round(time.monotonic() - begin, 4)}
    kind = "reset" if re.search(r"reset by peer|errno=104|errno=54", result.stderr, re.I) else "tls_or_http"
    raise ProbeError(kind)


def addresses():
    for resolver in DOH:
        try:
            result = run(["curl", "-4", "-fsS", "--noproxy", "*", "--connect-timeout", "3",
                          "--max-time", "8", "-H", "Accept: application/dns-json", resolver], timeout=10)
            answer = json.loads(result.stdout)
            if answer.get("Status") != 0 or answer.get("TC"):
                continue
            candidates = sorted({str(ipaddress.IPv4Address(item["data"]))
                                 for item in answer.get("Answer", []) if item.get("type") == 1
                                 and ipaddress.IPv4Address(item["data"]).is_global})
            if candidates:
                yield candidates[:8]
        except (GatewayError, ValueError, KeyError):
            continue


def verified_addresses(sni, ca):
    def validate(ip):
        try:
            return probe(ip, sni, ca)
        except ProbeError:
            return None

    for candidates in addresses():
        with ThreadPoolExecutor(max_workers=4) as pool:
            good = [check for check in pool.map(validate, candidates) if check]
        if good:
            return good
    raise GatewayError("no encrypted-DNS candidate passed TLS, hostname and GET 405 validation")


def sni_policy(ca, allow_workaround=False):
    try:
        return True, verified_addresses(True, ca), []
    except GatewayError:
        if not allow_workaround:
            raise
    deadline = time.monotonic() + 120
    for candidates in addresses():
        for ip in candidates:
            resets, successes, evidence = 0, [], []
            for _ in range(3):
                if time.monotonic() >= deadline:
                    raise GatewayError("SNI comparison time budget exhausted; exception not proven")
                try:
                    probe(ip, True, ca)
                    outcome = "connected"
                except ProbeError as error:
                    outcome = error.kind
                    resets += outcome == "reset"
                if outcome != "reset":
                    break
                try:
                    verified = probe(ip, False, ca)
                    successes.append(verified)
                    without = "verified_405"
                except ProbeError as error:
                    without = error.kind
                evidence.append({"ip": ip, "with_sni": outcome, "without_sni": without})
            if resets == 3 and len(successes) >= 2:
                return False, [successes[-1]], evidence
    raise GatewayError("same-IP comparison did not prove the SNI reset exception; SNI stays enabled")


def compose(root, *args):
    return run(["docker", "compose", "--project-directory", str(root), *args])


def inspect(container):
    return json.loads(run(["docker", "inspect", container]).stdout)[0]


def docker_python(config, code, *args, data=None):
    return run(["docker", "exec", "--user", "0", "-i", config["container"],
                config["python"], "-c", code, *args], data=data)


def map_queue(queue, mounts):
    matches = []
    for mount in mounts:
        destination = Path(mount["Destination"])
        if mount.get("Type") not in ("bind", "volume") or not mount.get("RW"):
            continue
        try:
            relative = Path(queue).relative_to(destination)
            matches.append((len(str(destination)), str(Path(mount["Source"]) / relative)))
        except ValueError:
            pass
    if not matches:
        raise GatewayError("notification queue is not on a writable persistent mount")
    return max(matches)[1]


DISCOVER = """
import inspect,json,sys,importlib.metadata
from chatmaild import metadata,notifier
from chatmaild.config import read_config
c=read_config(sys.argv[1])
assert callable(metadata.main)
assert hasattr(notifier.Notifier,'URL')
assert hasattr(notifier.Notifier,'requeue_persistent_queue_items')
assert hasattr(notifier.Notifier,'queue_for_retry')
assert hasattr(notifier.PersistentQueueItem,'create')
assert hasattr(notifier.PersistentQueueItem,'read_from_path')
assert 'requests.Session(' in inspect.getsource(notifier.NotifyThread.run)
try: version=importlib.metadata.version('chatmaild')
except importlib.metadata.PackageNotFoundError: version='unknown'
print(json.dumps({'domain':c.mail_domain,'queue':str(c.mailboxes_dir/'pending_notifications'),
                  'metadata_module':notifier.__file__,'package_version':version}))
"""


WRITE_RUNTIME = """
from pathlib import Path
import sys
p=Path(sys.argv[1])
missing=[]
parent=p.parent
while not parent.exists():
    missing.append(parent)
    parent=parent.parent
p.parent.mkdir(parents=True,exist_ok=True)
for parent in missing:
    parent.chmod(0o755)
p.write_text(sys.stdin.read())
p.chmod(0o644)
"""


def discover(root, service=None):
    project = json.loads(compose(root, "config", "--format", "json").stdout)
    names = [service] if service else list(project["services"])
    found = []
    for name in names:
        if name not in project["services"]:
            raise GatewayError("requested Compose service does not exist")
        ids = compose(root, "ps", "-q", name).stdout.split()
        for container_id in ids:
            container = inspect(container_id)
            if not container["State"].get("Running"):
                continue
            result = run(["docker", "exec", container_id, "systemctl", "show", SERVICE,
                          "-p", "MainPID", "--value"], check=False)
            if result.returncode or not result.stdout.strip().isdigit() or int(result.stdout) == 0:
                continue
            if container["HostConfig"]["NetworkMode"] != "host":
                raise GatewayError("mail container must use host networking for the loopback gateway")
            pid = int(result.stdout)
            argv = run(["docker", "exec", container_id, "cat", f"/proc/{pid}/cmdline"]).stdout.rstrip("\0").split("\0")
            if len(argv) != 4 or not Path(argv[0]).is_absolute() or Path(argv[1]).name != "chatmail-metadata":
                raise GatewayError("unsupported metadata entry point; audit existing overrides first")
            config = {"service": name, "container": container["Name"].lstrip("/"),
                      "image_id": container["Image"], "python": argv[0], "original_argv": argv}
            details = json.loads(docker_python(config, DISCOVER, argv[3]).stdout)
            config.update(details)
            config["queue_dir"] = map_queue(details["queue"], container["Mounts"])
            if not Path(config["queue_dir"]).is_dir():
                raise GatewayError("persistent notification queue directory is missing")
            config["original_unit"] = run(["docker", "exec", container_id, "systemctl", "cat", SERVICE]).stdout
            labels = container["Config"].get("Labels") or {}
            config["relay_revision"] = labels.get("org.opencontainers.image.revision", "unknown")
            config["compose_files"] = labels.get("com.docker.compose.project.config_files", "").split(",")
            found.append(config)
    if not found:
        raise MetadataNotReady("no running metadata service yet; start Chatmail and inspect its systemd services")
    if len(found) != 1:
        raise GatewayError("select one running metadata Compose service with --service")
    return found[0]


def queue_stats(config):
    ages = []
    now = time.time()
    for item in Path(config["queue_dir"]).iterdir():
        try:
            if item.is_file() and not item.name.endswith(".tmp"):
                ages.append(max(0, now - item.stat().st_mtime))
        except FileNotFoundError:
            pass
    return {"count": len(ages), "oldest_age_seconds": int(max(ages, default=0))}


def gateway_get(config, path="/notify", expected=405):
    try:
        result = run(["curl", "-sS", "--noproxy", "*", "--max-time", "12", "-o", "/dev/null",
                      "-w", "%{http_code}", f"http://127.0.0.1:{config['port']}{path}"], timeout=14)
        return result.stdout == str(expected)
    except GatewayError:
        return False


def nginx(config, *args):
    run(["docker", "exec", config["gateway"], "nginx", "-c", "/etc/chatmail-notify/nginx.conf", *args])


def upstream(checks):
    return "# Generated from validated encrypted DNS answers.\n" + "".join(
        f"server {check['ip']}:443 max_fails=2 fail_timeout=5s;\n" for check in checks)


def refresh(base, config):
    rollback_refresh(base, config)
    checks = verified_addresses(config["sni"], config["ca"])
    path = base / "nginx/upstream.inc"
    original = path.read_text()
    updated = upstream(checks)
    if updated != original:
        save(base / "refresh.pending", {"previous": original})
        atomic(path, updated, 0o644)
        try:
            nginx(config, "-t")
            nginx(config, "-s", "reload")
            if not any(gateway_get(config) for _ in range(3)):
                raise GatewayError("gateway failed verification after reload")
        except Exception:
            rollback_refresh(base, config)
            raise
    else:
        nginx(config, "-t")
    save(base / "last-verified.json", {"checked_at": int(time.time()), "checks": checks})
    (base / "refresh.pending").unlink(missing_ok=True)
    emit("upstream_verified", changed=updated != original, candidates=len(checks))


def rollback_refresh(base, config):
    pending = base / "refresh.pending"
    if pending.exists():
        atomic(base / "nginx/upstream.inc", read_json(pending)["previous"], 0o644)
        nginx(config, "-t")
        nginx(config, "-s", "reload")
        pending.unlink()
        emit("refresh_rolled_back")


def systemd_quote(value, environment=True):
    value = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if environment:
        value = value.replace("$", "$$")
    if "\n" in value or "\r" in value:
        raise GatewayError("newlines are not allowed in service arguments")
    return '"' + value + '"'


def dropin(config):
    argv = [config["python"], config["adapter"], *config["original_argv"][2:]]
    return ("# Managed by chatmail_deploy notification gateway.\n[Service]\n"
            f"Environment=CHATMAIL_NOTIFY_URL=http://127.0.0.1:{config['port']}/notify\n"
            "Environment=NO_PROXY=127.0.0.1,localhost\nEnvironment=no_proxy=127.0.0.1,localhost\n"
            "ExecStart=\nExecStart=" + " ".join(map(systemd_quote, argv)) + "\n")


def check_image(config):
    container = inspect(config["container"])
    if container["Image"] != config["image_id"]:
        raise GatewayError("mail image changed; disable and re-audit before re-enabling the adapter")
    if container["HostConfig"]["NetworkMode"] != "host":
        raise GatewayError("mail network mode changed")
    if map_queue(config["queue"], container["Mounts"]) != config["queue_dir"]:
        raise GatewayError("mail queue mount changed")


def apply_adapter(base, config):
    check_image(config)
    details = json.loads(docker_python(config, DISCOVER, config["original_argv"][3]).stdout)
    if any(details[key] != config[key] for key in ("domain", "queue")):
        raise GatewayError("mail identity or queue configuration changed; disable and re-audit")
    current = run(["docker", "exec", config["container"], "systemctl", "cat", SERVICE]).stdout
    section = "# " + config["dropin"] + "\n" + dropin(config)
    without_ours = current.replace("\n" + section, "").replace(section, "").rstrip()
    if without_ours != config["original_unit"].rstrip():
        raise GatewayError("metadata unit changed; preserve user overrides and re-audit")
    docker_python(config, WRITE_RUNTIME, config["adapter"], data=(base / "notification_metadata.py").read_text())
    docker_python(config, WRITE_RUNTIME, config["dropin"], data=dropin(config))
    run(["docker", "exec", config["container"], "systemctl", "daemon-reload"])
    run(["docker", "exec", config["container"], "systemctl", "restart", SERVICE])
    run(["docker", "exec", config["container"], "systemctl", "is-active", SERVICE])


def adapter_present(config):
    try:
        result = run(["docker", "exec", config["container"], "cat", config["dropin"]], check=False)
        if result.returncode or result.stdout != dropin(config):
            return False
        pid = run(["docker", "exec", config["container"], "systemctl", "show", SERVICE, "-p", "MainPID", "--value"]).stdout.strip()
        if not pid.isdigit() or int(pid) == 0:
            return False
        argv = run(["docker", "exec", config["container"], "cat", f"/proc/{pid}/cmdline"]).stdout.rstrip("\0").split("\0")
        return argv == [config["python"], config["adapter"], *config["original_argv"][2:]]
    except GatewayError:
        return False


def unit_paths(config):
    return [Path("/etc/systemd/system") / (config["unit"] + suffix) for suffix in
            ("-apply.service", "-health.service", "-health.timer", "-refresh.service", "-refresh.timer")]


def install_units(base, config):
    executable = " ".join(map(systemd_quote, [sys.executable, base / "notification_gateway.py", "--root", config["root"]]))
    common = ("[Unit]\nRequires=docker.service\nAfter=docker.service network-online.target\n"
              "[Service]\nType=oneshot\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
              "ProtectSystem=strict\nProtectHome=false\nTimeoutStartSec=300\n"
              "ReadWritePaths=" + systemd_quote(base, environment=False) + "\n")
    contents = [common + f"ExecStart={executable} apply\n[Install]\nWantedBy=multi-user.target\n",
                common + f"ExecStart={executable} health\n",
                "[Timer]\nOnCalendar=*-*-* *:*:00\nAccuracySec=1s\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
                common + f"ExecStart={executable} refresh\n",
                "[Timer]\nOnCalendar=hourly\nRandomizedDelaySec=120s\nPersistent=true\n[Install]\nWantedBy=timers.target\n"]
    if any(path.exists() for path in unit_paths(config)):
        raise GatewayError("refusing to replace an existing maintenance unit")
    for path, content in zip(unit_paths(config), contents):
        atomic(path, "# Managed by chatmail_deploy notification gateway.\n" + content, 0o644)
    run(["systemd-analyze", "verify", *map(str, unit_paths(config))])
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", config["unit"] + "-apply.service"])
    run(["systemctl", "enable", "--now", config["unit"] + "-health.timer", config["unit"] + "-refresh.timer"])


def gateway_compose(base, config, image):
    service = {"image": image, "container_name": config["gateway"], "user": "101:101",
               "entrypoint": ["/usr/sbin/nginx"], "command": ["-c", "/etc/chatmail-notify/nginx.conf", "-g", "daemon off;"],
               "network_mode": "host", "read_only": True, "restart": "unless-stopped",
               "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m,mode=1777"], "cap_drop": ["ALL"],
               "security_opt": ["no-new-privileges:true"], "pids_limit": 64, "mem_limit": "128m",
               "volumes": [{"type": "bind", "source": str(base / "nginx"), "target": "/etc/chatmail-notify", "read_only": True},
                           {"type": "bind", "source": config["ca"], "target": "/etc/ssl/certs/ca-certificates.crt", "read_only": True}],
               "healthcheck": {"test": ["CMD", "wget", "-q", "-O", "/dev/null", f"http://127.0.0.1:{config['port']}/healthz"],
                               "interval": "30s", "timeout": "3s", "retries": 3, "start_period": "5s"},
               "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}}
    return {"name": config["unit"], "services": {"gateway": service}}


def gateway_command(base, *args):
    return run(["docker", "compose", "-f", str(base / "compose.json"), *args], timeout=120)


def restore_adapter(config):
    # Removing our dedicated drop-in restores the original ExecStart; no mail data is touched.
    check_image(config)
    result = run(["docker", "exec", config["container"], "cat", config["dropin"]], check=False)
    if result.returncode == 0 and result.stdout != dropin(config):
        raise GatewayError("gateway drop-in was modified; manual merge required")
    run(["docker", "exec", config["container"], "rm", "-f", config["dropin"]])
    run(["docker", "exec", config["container"], "systemctl", "daemon-reload"])
    run(["docker", "exec", config["container"], "systemctl", "restart", SERVICE])
    run(["docker", "exec", config["container"], "systemctl", "is-active", SERVICE])


def disable(base, config):
    for suffix in ("-health.timer", "-refresh.timer", "-apply.service"):
        run(["systemctl", "disable", "--now", config["unit"] + suffix], check=False)
    for suffix in ("-health.service", "-refresh.service"):
        run(["systemctl", "stop", config["unit"] + suffix], check=False)
    if config.get("enabled") or config.get("phase") == "applying":
        restore_adapter(config)
    if (base / "compose.json").exists():
        gateway_command(base, "stop", "gateway")
    for path in unit_paths(config):
        if path.exists() and path.read_text().startswith("# Managed by chatmail_deploy notification gateway.\n"):
            path.unlink()
    run(["systemctl", "daemon-reload"])
    config["enabled"] = False
    config["phase"] = "disabled"
    save(base / "config.json", config)
    emit("disabled", queue_preserved=True)


def enable(root, base, args):
    if (base / "config.json").exists():
        previous = read_json(base / "config.json")
        if previous.get("managed_by") != "chatmail_deploy/notification-gateway-v1":
            raise GatewayError("existing gateway directory belongs to another deployment; audit it first")
        if previous.get("enabled") or previous.get("phase") != "disabled":
            raise GatewayError("gateway already enabled or partially applied; disable before reconfiguration")
    elif any(path.name != "operations.lock" for path in base.iterdir()):
        raise GatewayError("refusing to overwrite an existing gateway directory")
    deadline = time.monotonic() + 90
    while True:
        try:
            config = discover(root, args.service)
            break
        except MetadataNotReady:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", args.port))
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:10]
    config.update(root=str(root), managed_by="chatmail_deploy/notification-gateway-v1", port=args.port, unit="chatmail-notification-" + identity,
                  gateway="chatmail-notification-gateway-" + identity, ca=ca_bundle(), enabled=False,
                  adapter=f"/usr/local/lib/chatmail-notification/{identity}/notification_metadata.py",
                  dropin=f"/etc/systemd/system/{SERVICE}.d/90-notification-{identity}.conf")
    if any(path.exists() for path in unit_paths(config)):
        raise GatewayError("maintenance units already exist; disable or audit the existing installation")
    config["sni"], checks, config["sni_evidence"] = sni_policy(config["ca"], args.allow_sni_workaround)
    if run(["docker", "exec", config["container"], "test", "-e", config["dropin"]], check=False).returncode == 0:
        raise GatewayError("dedicated gateway drop-in already exists; audit before enabling")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = base / ("backup-" + str(time.time_ns()))
    config["backup"] = str(backup)
    config["phase"] = "preparing"
    save(base / "config.json", config)
    backup.mkdir(mode=0o700)
    atomic(backup / "metadata.service", config["original_unit"])
    for index, path in enumerate(config["compose_files"]):
        if path and Path(path).is_file():
            atomic(backup / f"compose-{index}.yaml", Path(path).read_text())
    for name in ("notification_gateway.py", "notification_metadata.py"):
        source = HERE / name
        if source.resolve() != (base / name).resolve():
            atomic(base / name, source.read_text())
    nginx_dir = base / "nginx"
    nginx_dir.mkdir(mode=0o755, exist_ok=True)
    template_path = HERE / "config/notification-nginx.conf"
    if not template_path.exists():
        template_path = HERE / "notification-nginx.conf.template"
    template = template_path.read_text()
    atomic(base / "notification-nginx.conf.template", template)
    atomic(nginx_dir / "nginx.conf", template.replace("@@PORT@@", str(args.port)).replace("@@SNI@@", "on" if config["sni"] else "off"), 0o644)
    atomic(nginx_dir / "upstream.inc", upstream(checks), 0o644)
    run(["docker", "pull", args.gateway_image], timeout=300)
    image = json.loads(run(["docker", "image", "inspect", args.gateway_image]).stdout)[0]
    pinned = next(iter(image.get("RepoDigests") or []), image["Id"])
    atomic(base / "compose.json", json.dumps(gateway_compose(base, config, pinned), indent=2))
    save(base / "config.json", config)
    gateway_command(base, "config", "--quiet")
    applied = False
    try:
        gateway_command(base, "up", "-d", "--no-build", "--pull", "never", "gateway")
        for _ in range(10):
            if gateway_get(config, "/healthz", 200):
                break
            time.sleep(0.5)
        else:
            raise GatewayError("gateway did not become locally ready")
        nginx(config, "-t")
        if not any(gateway_get(config) for _ in range(3)):
            raise GatewayError("gateway startup or upstream verification failed")
        applied = True  # Restore even if writing the drop-in or restarting fails halfway.
        config["phase"] = "applying"
        save(base / "config.json", config)
        apply_adapter(base, config)
        config["enabled"] = True
        config["phase"] = "enabled"
        save(base / "config.json", config)
        install_units(base, config)
    except Exception:
        for suffix in ("-health.timer", "-refresh.timer", "-apply.service"):
            run(["systemctl", "disable", "--now", config["unit"] + suffix], check=False)
        if applied:
            restore_adapter(config)
        gateway_command(base, "stop", "gateway")
        for path in unit_paths(config):
            if path.exists() and path.read_text().startswith("# Managed by chatmail_deploy notification gateway.\n"):
                path.unlink()
        run(["systemctl", "daemon-reload"], check=False)
        config["enabled"] = False
        config["phase"] = "disabled"
        save(base / "config.json", config)
        raise
    emit("enabled", gateway=config["gateway"], sni=config["sni"], backup=str(backup), queue=queue_stats(config))


def failed(state, key, healthy):
    state[key] = 0 if healthy else state.get(key, 0) + 1
    return state[key] >= 3


def cooled(state, key, now, container=None):
    if now - state.get(key, 0) < 600:
        return False
    if container:
        try:
            started = datetime.datetime.fromisoformat(container["State"]["StartedAt"][:19] + "+00:00").timestamp()
            if now - started < 600:
                return False
        except (ValueError, KeyError):
            return False  # Unknown startup time is not permission for a restart.
    return True


def health(base, config):
    now = int(time.time())
    state_path = base / "health.json"
    state = read_json(state_path) if state_path.exists() else {}
    def state_of(name):
        try:
            return inspect(name)
        except GatewayError:
            return {"State": {"Running": False}}

    mail, gateway = state_of(config["container"]), state_of(config["gateway"])
    running = mail["State"].get("Running", False)
    mail_ok = running and mail["State"].get("Health", {}).get("Status", "healthy") == "healthy"
    gateway_ok = (gateway["State"].get("Running", False)
                  and gateway["State"].get("Health", {}).get("Status", "healthy") == "healthy"
                  and gateway_get(config, "/healthz", 200))
    try:
        result = run(["docker", "exec", config["container"], "systemctl", "is-active", "postfix", "dovecot", SERVICE], check=False)
        services = result.stdout.splitlines()
    except GatewayError:
        services = []
    metadata_ok = len(services) == 3 and services[2] == "active"
    adapter_ok = metadata_ok and adapter_present(config)
    upstream_ok = gateway_ok and gateway_get(config)
    try:
        queue = queue_stats(config)
    except OSError:
        queue = {"count": -1, "oldest_age_seconds": -1}
    failures = {key: failed(state, key + "_failures", ok) for key, ok in
                (("mail", mail_ok), ("gateway", gateway_ok), ("metadata", adapter_ok),
                 ("services", services == ["active"] * 3), ("upstream", upstream_ok),
                 ("queue", queue["count"] >= 0 and queue["oldest_age_seconds"] < 300))}

    def recover(key, action, container=None):
        if cooled(state, key + "_recovery", now, container):
            state[key + "_recovery"] = now
            save(state_path, state)
            try:
                action()
                emit("recovery_attempt", component=key, success=True)
            except GatewayError:
                emit("recovery_attempt", component=key, success=False)

    if failures["mail"]:
        recover("mail", lambda: run(["docker", "restart" if running else "start", config["container"]]), mail)
    if failures["gateway"]:
        recover("gateway", lambda: run(["docker", "restart", config["gateway"]]), gateway)
    if running and failures["metadata"]:
        recover("metadata", lambda: apply_adapter(base, config))
    elif running and failures["queue"] and metadata_ok and upstream_ok:
        recover("metadata", lambda: run(["docker", "exec", config["container"], "systemctl", "restart", SERVICE]))
    if running and failures["services"] and len(services) == 3:
        targets = [name for name, status in zip(("postfix", "dovecot"), services) if status != "active"]
        if targets:
            recover("services", lambda: run(["docker", "exec", config["container"], "systemctl", "restart", *targets]))
    if gateway_ok and failures["upstream"]:
        # Reuse the current lock; spawning another locked unit races our own exit.
        recover("upstream", lambda: refresh(base, config))
    state.update(checked_at=now, queue=queue, mail_ok=mail_ok, gateway_ok=gateway_ok,
                 metadata_ok=metadata_ok, adapter_ok=adapter_ok, upstream_ok=upstream_ok)
    save(state_path, state)
    emit("health", mail=mail_ok, gateway=gateway_ok, metadata=metadata_ok, adapter=adapter_ok, upstream=upstream_ok, queue=queue)
    return all((mail_ok, gateway_ok, metadata_ok, adapter_ok, upstream_ok, services == ["active"] * 3,
                queue["count"] >= 0 and queue["oldest_age_seconds"] < 300))


def main():
    if sys.version_info < (3, 9):
        raise GatewayError("Python 3.9 or newer is required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Actual Chatmail Compose directory")
    parser.add_argument("--service", help="Mail Compose service; otherwise discover exactly one metadata service")
    parser.add_argument("--port", default=10089, type=int)
    parser.add_argument("--gateway-image", default="nginx:stable-alpine")
    parser.add_argument("--allow-sni-workaround", action="store_true", help="Permit a no-SNI gateway ONLY after repeated same-IP reset/verified-405 comparisons")
    parser.add_argument("action", choices=("audit", "enable", "disable", "status", "apply", "refresh", "health"))
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("gateway port must be between 1024 and 65535")
    root = args.root.resolve()
    if not root.is_dir():
        raise GatewayError("Compose root does not exist")
    base = root / "notification-gateway"
    if args.action == "audit":
        config = discover(root, args.service)
        try:
            dns = sorted({entry[4][0] for entry in socket.getaddrinfo(HOST, 443, socket.AF_INET, socket.SOCK_STREAM)})
        except OSError:
            dns = []
        services = run(["docker", "exec", config["container"], "systemctl", "is-active", "postfix", "dovecot", SERVICE], check=False).stdout.splitlines()
        services = [status if status in ("active", "inactive", "failed", "unknown", "activating", "deactivating") else "unknown" for status in services]
        try:
            checks = verified_addresses(True, ca_bundle())
            endpoint = {"verified": True, "checks": checks}
        except GatewayError:
            endpoint = {"verified": False}
        emit("audit", root=str(root), service=config["service"], container=config["container"],
             domain=config["domain"], queue_dir=config["queue_dir"], queue=queue_stats(config),
             services=services, image_id=config["image_id"], relay_revision=config["relay_revision"],
             metadata_module=config["metadata_module"], package_version=config["package_version"],
             system_dns_ipv4=dns, endpoint=endpoint)
        return
    if args.action == "status":
        config = read_json(base / "config.json")
        emit("status", enabled=config["enabled"], container=config["container"], gateway=config["gateway"],
             unit=config["unit"], port=config["port"], sni=config["sni"], queue=queue_stats(config))
        return
    if os.geteuid() != 0:
        raise GatewayError("mutating/maintenance actions require root")
    if base.is_symlink() or (base.exists() and base.stat().st_uid != 0):
        raise GatewayError("gateway state directory must be a root-owned real directory")
    base.mkdir(mode=0o700, exist_ok=True)
    base.chmod(0o700)
    with (base / "operations.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GatewayError("another gateway operation is running") from None
        if args.action == "enable":
            enable(root, base, args)
        else:
            config = read_json(base / "config.json")
            if config.get("root") != str(root) or config.get("managed_by") != "chatmail_deploy/notification-gateway-v1":
                raise GatewayError("gateway configuration belongs to another deployment")
            if args.action != "disable" and not config.get("enabled"):
                raise GatewayError("gateway is not enabled")
            outcome = {"disable": disable, "apply": apply_adapter, "refresh": refresh, "health": health}[args.action](base, config)
            if args.action == "health" and not outcome:
                return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (GatewayError, OSError, ValueError, KeyError) as error:
        emit("operation_failed", error_type=type(error).__name__, reason=str(error) if isinstance(error, GatewayError) else "check deployment configuration")
        sys.exit(1)
