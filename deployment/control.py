"""Generate fleet config, supervise read-only SSH forwards, and check scrapes."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.parse


def port(value):
    if isinstance(value, bool) or not str(value).isdigit() or not 1024 <= int(value) <= 65535:
        raise ValueError(f"Expected an unprivileged TCP port, got {value!r}")
    return int(value)


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError("Addresses must be IPv4 or DNS names without whitespace/options")
    if "REPLACE" in value.upper():
        raise ValueError("Replace the example address/user placeholders first")
    return value


def endpoint(value):
    host, separator, number = value.rpartition(":")
    if not separator:
        raise ValueError("Metric endpoints must be HOST:PORT")
    return f"{address(host)}:{port(number)}"


def vc_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"validator-[1-9][0-9]*", value):
        raise ValueError("VC ids must be validator-1, validator-2, etc.")
    return value


def display_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}", value):
        raise ValueError("VC names may contain letters, numbers, spaces, dots, underscores and hyphens")
    return value


def metrics_path(value):
    if not re.fullmatch(r"/[A-Za-z0-9_./-]*", value):
        raise ValueError("Invalid metrics path")
    return value


def settings(env):
    own_id = vc_id(env.get("VC_ID", "validator-1"))
    own_name = display_name(env.get("VC_NAME", "VC 1"))
    local_vc = endpoint(env.get("VC_METRICS_ADDRESS", "127.0.0.1:8808"))
    node_port = port(env.get("NODE_EXPORTER_PORT", "19103"))
    prom_port = port(env.get("PROMETHEUS_PORT", "9092"))
    grafana_port = port(env.get("GRAFANA_PORT", "3200"))
    backend = address(env["BACKEND_ADDRESS"])
    inventory_enabled = env.get("INVENTORY_ENABLED", "false")
    if inventory_enabled not in ("true", "false"):
        raise ValueError("INVENTORY_ENABLED must be true or false")
    inventory_port = port(env.get("INVENTORY_PORT", "19200"))
    beacon_apis = [f"http://{backend}:{port(env.get(key, default))}" for key, default in
                   (("MAIN_BEACON_API_PORT", "5052"), ("FALLBACK_BEACON_API_PORT", "5552"))]
    interval = env.get("SCRAPE_INTERVAL", "30s")
    if not re.fullmatch(r"[1-9][0-9]*s", interval) or int(interval[:-1]) < 15:
        raise ValueError("SCRAPE_INTERVAL must be at least 15s, expressed in seconds")
    backends = []
    for role, prefix, defaults in (("main", "MAIN", (18008, 19105, 49103)),
                                    ("fallback", "FALLBACK", (28008, 29105, 59103))):
        for component, suffix, default in zip(("eth2", "eth1", "node"),
                                               ("BEACON", "EXECUTION", "HOST"), defaults):
            path = metrics_path(env.get(f"{prefix}_EXECUTION_PATH", "/metrics" if role == "main" else "/debug/metrics/prometheus")) if component == "eth1" else "/metrics"
            number = port(env.get(f"{prefix}_{suffix}_PORT", str(default)))
            backends.append((role, component, f"{backend}:{number}", path))
    # Reserve existing monitoring endpoints too, to avoid shadowing loopback
    # addresses or colliding with broadly bound host listeners.
    occupied = [inventory_port, node_port, prom_port, grafana_port, int(local_vc.rsplit(":", 1)[1])]
    occupied += [int(item[2].rsplit(":", 1)[1]) for item in backends]
    occupied += [int(url.rsplit(":", 1)[1]) for url in beacon_apis]
    if len(set(occupied)) != len(occupied):
        raise ValueError("Local monitoring ports must be distinct")
    children = json.loads(env.get("CHILDREN", "[]"))
    if not isinstance(children, list):
        raise ValueError("CHILDREN must be a JSON list")
    seen = {own_id}
    normalized = []
    for child in children:
        ident = vc_id(child["id"])
        if ident in seen:
            raise ValueError(f"Duplicate VC id: {ident}")
        seen.add(ident)
        base = port(child["local_port"])
        port(base + 1)
        if base in occupied or base + 1 in occupied:
            raise ValueError(f"Child forward port collision: {base}")
        occupied += [base, base + 1]
        ssh_port = child.get("port", 22)
        if isinstance(ssh_port, bool) or not str(ssh_port).isdigit() or not 1 <= int(ssh_port) <= 65535:
            raise ValueError("Invalid SSH port")
        normalized.append({
            "id": ident, "name": display_name(child.get("name", ident)),
            "host": address(child["host"]), "user": address(child["user"]),
            "port": int(ssh_port), "local_port": base,
            "vc_address": address(child.get("vc_address", "127.0.0.1")),
            "vc_port": port(child.get("vc_port", 8808)),
            "node_address": address(child.get("node_address", "127.0.0.1")),
            "node_port": port(child.get("node_port", 19103)),
        })
    return {"own_id": own_id, "own_name": own_name, "local_vc": local_vc,
            "node_port": node_port, "prom_port": prom_port, "grafana_port": grafana_port,
            "interval": interval, "children": normalized, "backends": backends,
            "inventory_enabled": inventory_enabled == "true", "inventory_port": inventory_port,
            "beacon_apis": beacon_apis}


def targets(config):
    result = []

    def add(host, role, component, target, path="/metrics"):
        result.append({"targets": [target], "labels": {
            "host": host, "role": role, "component": component, "__metrics_path__": path}})

    add(config["own_id"], "validator", "validator", config["local_vc"])
    add(config["own_id"], "validator", "node", f"127.0.0.1:{config['node_port']}")
    for child in config["children"]:
        add(child["id"], "validator", "validator", f"127.0.0.1:{child['local_port']}")
        add(child["id"], "validator", "node", f"127.0.0.1:{child['local_port'] + 1}")
    for host, component, target, path in config["backends"]:
        add(host, "backend", component, target, path)
    if config['inventory_enabled']:
        result.append({"targets": [f"127.0.0.1:{config['inventory_port']}"], "labels": {
            "role": "monitoring", "component": "inventory", "__metrics_path__": "/metrics"}})
    return result


def write_json(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(content, indent=2) + "\n")
    temporary.chmod(0o644)
    temporary.replace(path)


def client_labels(item, clients):
    """Replace the example fleet's display overrides and table value mappings."""
    if isinstance(item, list):
        for child in item:
            client_labels(child, clients)
    elif isinstance(item, dict):
        if any(target.get("legendFormat") == "{{host}} · {{mountpoint}}" for target in item.get("targets", [])):
            # Storage names include a mountpoint, so whole-field byName
            # overrides do not match. Use labels to keep the mount suffix.
            item["transformations"] = [t for t in item.get("transformations", [])
                                       if not (t.get("id") == "renameByRegex"
                                               and t.get("options", {}).get("regex", "").startswith("^validator-"))]
            storage_overrides = item.setdefault("fieldConfig", {}).setdefault("overrides", [])
            storage_overrides.extend([
                {"matcher": {"id": "byRegexp", "options": "^" + re.escape(ident) + " · "},
                 "properties": [{"id": "displayName", "value": name + " · ${__field.labels.mountpoint}"}]}
                for ident, name in clients])
        overrides = item.get("overrides", [])
        def is_client_override(override):
            matcher = override.get("matcher", {})
            return matcher.get("id") == "byName" and re.fullmatch(r"validator-[1-9][0-9]*", str(matcher.get("options", "")))
        if any(is_client_override(override) for override in overrides):
            previous = {o["matcher"]["options"]: o for o in overrides if is_client_override(o)}
            replacement = []
            for ident, name in clients:
                properties = [p for p in previous.get(ident, {}).get("properties", []) if p["id"] != "displayName"]
                replacement.append({"matcher": {"id": "byName", "options": ident},
                                    "properties": properties + [{"id": "displayName", "value": name}]})
            item["overrides"] = [o for o in overrides if not is_client_override(o)] + replacement
        if item.get("type") == "value" and isinstance(item.get("options"), dict):
            options = item["options"]
            old_ids = [key for key in options if re.fullmatch(r"validator-[1-9][0-9]*", key)]
            if old_ids:
                preserved = {ident: options[ident] for ident in old_ids}
                for ident in old_ids:
                    del options[ident]
                for ident, name in clients:
                    options[ident] = {**preserved.get(ident, {}), "text": name}
        for child in item.values():
            client_labels(child, clients)


def generate(config, output, template):
    # JSON is valid YAML; .yaml names allow Grafana's provisioning loader to find it.
    write_json(output / "targets.json", targets(config))
    write_json(output / "prometheus.json", {
        "global": {"scrape_interval": config["interval"], "scrape_timeout": "10s", "evaluation_interval": config["interval"]},
        "scrape_configs": [
            {"job_name": "prometheus", "static_configs": [{"targets": [f"127.0.0.1:{config['prom_port']}"]}]},
            {"job_name": "fleet", "fallback_scrape_protocol": "PrometheusText0.0.4",
             "file_sd_configs": [{"files": ["/generated/targets.json"], "refresh_interval": "30s"}],
             "relabel_configs": [{"source_labels": ["component"], "target_label": "job"}]}
        ]})
    write_json(output / "provisioning/datasources/fleet.yaml", {
        "apiVersion": 1, "datasources": [{"name": "Lido Prometheus", "uid": "lido-prometheus",
            "type": "prometheus", "access": "proxy", "url": f"http://127.0.0.1:{config['prom_port']}",
            "isDefault": True, "editable": False,
            "jsonData": {"timeInterval": config["interval"], "httpMethod": "POST"}}]})
    write_json(output / "provisioning/dashboards/fleet.yaml", {
        "apiVersion": 1, "providers": [{"name": "Lido fleet", "orgId": 1, "folder": "Lido", "type": "file",
            "disableDeletion": True, "updateIntervalSeconds": 30, "allowUiUpdates": False,
            "options": {"path": "/generated/dashboards"}}]})
    dashboard = json.loads(template.read_text())
    clients = [(config["own_id"], config["own_name"])] + [(c["id"], c["name"]) for c in config["children"]]
    variable = next(v for v in dashboard["templating"]["list"] if v["name"] == "validator")
    variable["query"] = ", ".join(f"{name} : {ident}" for ident, name in clients)
    variable["options"] = [{"text": "All", "value": "$__all", "selected": True}] + [
        {"text": name, "value": ident, "selected": False} for ident, name in clients]
    variable["current"] = {"text": "All", "value": ["$__all"]}
    client_labels(dashboard, clients)
    dashboard["description"] = "Nimbus validator fleet with shared main and fallback beacon/execution nodes. Loaded keys are not an on-chain active validator count."
    write_json(output / "dashboards/lido-validator-fleet.json", dashboard)


def ssh_command(child):
    command = ["ssh", "-F", "/dev/null", "-N", "-T", "-p", str(child["port"]),
               "-l", child["user"], "-i", "/run/ssh/id"]
    for option in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                   "UserKnownHostsFile=/run/ssh/known_hosts", "GlobalKnownHostsFile=/dev/null",
                   "ExitOnForwardFailure=yes", "ConnectTimeout=10", "ServerAliveInterval=15",
                   "ServerAliveCountMax=2", "ForwardAgent=no", "ForwardX11=no", "LogLevel=ERROR"):
        command += ["-o", option]
    for offset, kind in ((0, "vc"), (1, "node")):
        command += ["-L", f"127.0.0.1:{child['local_port'] + offset}:{child[kind + '_address']}:{child[kind + '_port']}"]
    return command + [child["host"]]


def supervise(config):
    for path in (Path("/run/ssh/id"), Path("/run/ssh/known_hosts")):
        if not path.is_file():
            raise ValueError(f"Missing SSH file: {path}")
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    running = {}
    retry_at = {}
    try:
        while not stopped:
            for child in config["children"]:
                ident = child["id"]
                process = running.get(ident)
                if process is not None and process.poll() is not None:
                    print(f"{ident}: SSH exited ({process.returncode}); retrying in 5s", flush=True)
                    running.pop(ident)
                    retry_at[ident] = time.monotonic() + 5
                if ident not in running and time.monotonic() >= retry_at.get(ident, 0):
                    print(f"{ident}: starting metrics forwards", flush=True)
                    running[ident] = subprocess.Popen(ssh_command(child))
            time.sleep(1)
    finally:
        for process in running.values():
            if process.poll() is None:
                process.terminate()
        for process in running.values():
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def check(config):
    # Queries Prometheus's own scrape results, rather than merely curling the
    # host endpoints. This reports reachability from the actual scraper.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def fetch(url):
        with opener.open(url, timeout=10) as response:
            return json.load(response)

    result = fetch(f"http://127.0.0.1:{config['prom_port']}/api/v1/targets")
    if result.get("status") != "success":
        raise ValueError("Prometheus targets API did not succeed")
    active = result["data"]["activeTargets"]
    failed = False
    for target in targets(config):
        labels = target["labels"]
        matches = [t for t in active if all(t.get("labels", {}).get(key) == labels[key]
                   for key in ("host", "role", "component") if key in labels)]
        if len(matches) != 1:
            print(f"FAIL {labels.get('host', 'collector')} / {labels['component']}: expected one target, found {len(matches)}")
            failed = True
        else:
            target_state = matches[0]
            scrape_url = urllib.parse.urlsplit(target_state.get("scrapeUrl", ""))
            ok = (target_state["health"] == "up"
                  and target_state.get("labels", {}).get("job") == labels["component"]
                  and target_state.get("labels", {}).get("instance") == target["targets"][0]
                  and scrape_url.path == labels["__metrics_path__"])
            print(f"{'OK' if ok else 'FAIL'} {labels.get('host', 'collector')} / {labels['component']}: {target_state['health']} {target_state.get('lastError', '')}" + (" (check address/path/job against .env)" if not ok else ""))
            failed |= not ok
    if config['inventory_enabled']:
        expression = 'lido_inventory_success{job="inventory"} == 1 and on(host) (time() - lido_inventory_timestamp_seconds{job="inventory"} < 180)'
        data = fetch(f"http://127.0.0.1:{config['prom_port']}/api/v1/query?" + urllib.parse.urlencode({'query': expression}))
        healthy = {item['metric'].get('host') for item in data.get('data', {}).get('result', [])}
        for ident in [config['own_id']] + [c['id'] for c in config['children']]:
            ok = data.get('status') == 'success' and ident in healthy
            print(f"{'OK' if ok else 'FAIL'} {ident} / inventory: " + ('fresh beacon snapshot' if ok else 'missing, failed or stale CSV/beacon lookup'))
            failed |= not ok
    health = fetch(f"http://127.0.0.1:{config['grafana_port']}/api/health")
    grafana_ok = health.get("database") == "ok"
    print(f"{'OK' if grafana_ok else 'FAIL'} Grafana database")
    print("Scrape health does not establish metric coverage, filesystem accuracy, or validator duty success.")
    return 1 if failed or not grafana_ok else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("generate", "tunnels", "inventory", "check"))
    parser.add_argument("--output", type=Path, default=Path("/generated"))
    parser.add_argument("--template", type=Path, default=Path("/app/dashboard.json"))
    args = parser.parse_args()
    config = settings(os.environ)
    if args.mode == "generate":
        generate(config, args.output, args.template)
        print(f"Generated configuration for {1 + len(config['children'])} VCs and two shared backends")
    elif args.mode == "inventory":
        import inventory
        inventory.serve(config)
    elif args.mode == "tunnels":
        supervise(config)
    else:
        return check(config)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError) as error:
        print(f"Configuration/check failed: {error}", file=sys.stderr)
        sys.exit(1)
