import contextlib
import importlib.util
import io
import json
import re
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("control", ROOT / "deployment/control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


def environment():
    return {"BACKEND_ADDRESS": "192.0.2.10", "VC_ID": "validator-1", "VC_NAME": "VC 1 - Hub",
            "CHILDREN": json.dumps([
                {"id": "validator-2", "host": "child2.example.org", "user": "monitor", "port": 222, "local_port": 20000},
                {"id": "validator-3", "host": "child3.example.org", "user": "monitor", "local_port": 20002}])}


class Configuration(unittest.TestCase):
    def test_exact_targets_and_geth_path(self):
        config = control.settings(environment())
        targets = control.targets(config)
        self.assertEqual(len(targets), 12)
        self.assertEqual(len({(t['labels']['host'], t['labels']['component']) for t in targets}), 12)
        fallback = next(t for t in targets if t['labels']['host'] == 'fallback' and t['labels']['component'] == 'eth1')
        self.assertEqual(fallback['targets'], ['192.0.2.10:29105'])
        self.assertEqual(fallback['labels']['__metrics_path__'], '/debug/metrics/prometheus')
        child = next(t for t in targets if t['labels']['host'] == 'validator-3' and t['labels']['component'] == 'node')
        self.assertEqual(child['targets'], ['127.0.0.1:20003'])

    def test_reject_duplicate_ids_and_overlapping_or_reserved_ports(self):
        for field, value in [('id', 'validator-1'), ('id', 'validator-2'), ('local_port', 20001),
                             ('local_port', 9092), ('local_port', 19102), ('local_port', 65535),
                             ('host', '-oProxyCommand=evil'), ('user', 'bad user')]:
            env = environment()
            children = json.loads(env['CHILDREN'])
            children[1][field] = value
            env['CHILDREN'] = json.dumps(children)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                control.settings(env)

    def test_ssh_forwards_are_private_and_require_known_keys(self):
        child = control.settings(environment())['children'][0]
        command = control.ssh_command(child)
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('ForwardAgent=no', command)
        self.assertIn('BatchMode=yes', command)
        self.assertEqual([command[i + 1] for i, item in enumerate(command) if item == '-L'],
                         ['127.0.0.1:20000:127.0.0.1:8808', '127.0.0.1:20001:127.0.0.1:19103'])
        # OpenSSH parses the actual generated arguments without connecting.
        result = subprocess.run([command[0], '-G', *command[1:]], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_dashboard_tracks_members_and_datasource_interval(self):
        env = environment()
        children = json.loads(env['CHILDREN'])
        children.append({'id': 'validator-4', 'host': 'child4.example.org', 'user': 'monitor', 'local_port': 20004})
        env['CHILDREN'] = json.dumps(children)
        config = control.settings(env)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            template = ROOT / 'monitoring/dashboards/lido-validator-fleet.json'
            control.generate(config, output, template)
            board = json.loads((output / 'dashboards/lido-validator-fleet.json').read_text())
            variable = next(v for v in board['templating']['list'] if v['name'] == 'validator')
            self.assertIn('validator-4', variable['query'])
            self.assertEqual(len(variable['options']), 5)
            source = json.loads((output / 'provisioning/datasources/fleet.yaml').read_text())
            self.assertEqual(source['datasources'][0]['jsonData']['timeInterval'], '30s')
            self.assertEqual(source['datasources'][0]['url'], 'http://127.0.0.1:9092')

    def test_fleet_grows_and_shrinks_without_leftover_clients(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            # Reuse the same output directory to exercise configuration changes.
            for count in (1, 2, 3, 5, 2):
                with self.subTest(clients=count):
                    env = environment()
                    env['CHILDREN'] = json.dumps([
                        {'id': f'validator-{i}', 'name': f'VC {i} - Example',
                         'host': f'child{i}.example.org', 'user': 'monitor', 'local_port': 20000 + (i - 2) * 2}
                        for i in range(2, count + 1)])
                    config = control.settings(env)
                    control.generate(config, output, ROOT / 'monitoring/dashboards/lido-validator-fleet.json')
                    board_text = (output / 'dashboards/lido-validator-fleet.json').read_text()
                    board = json.loads(board_text)
                    expected = {f'validator-{i}' for i in range(1, count + 1)}
                    self.assertEqual(set(re.findall(r'validator-[1-9][0-9]*', board_text)), expected)
                    variable = next(v for v in board['templating']['list'] if v['name'] == 'validator')
                    self.assertEqual({v['value'] for v in variable['options'] if v['value'] != '$__all'}, expected)
                    self.assertEqual(variable['current']['value'], ['$__all'])
                    rows = [p for p in board['panels'] if p.get('repeat') == 'validator']
                    self.assertEqual(len(rows), 1)  # Grafana repeats this template for each selection.
                    self.assertIn('${validator:text}', rows[0]['title'])
                    table = next(p for p in board['panels'] if p['id'] == 38)
                    instance = next(o for o in table['fieldConfig']['overrides'] if o['matcher'].get('options') == 'Instance')
                    mappings = next(p['value'] for p in instance['properties'] if p['id'] == 'mappings')
                    self.assertEqual(set(mappings[0]['options']), expected)
                    if count >= 2:
                        self.assertEqual(mappings[0]['options']['validator-2']['text'], 'VC 2 - Example')
                    targets = json.loads((output / 'targets.json').read_text())
                    self.assertEqual(len(targets), count * 2 + 6)
                    self.assertEqual({t['labels']['host'] for t in targets if t['labels']['role'] == 'validator'}, expected)

    def test_custom_names_cover_tables_charts_and_storage_without_changing_series(self):
        env = environment()
        env['VC_NAME'] = 'Apple'
        children = json.loads(env['CHILDREN'])
        for child, name in zip(children, ['Bananas', 'Oranges']):
            child['name'] = name
        env['CHILDREN'] = json.dumps(children)
        config = control.settings(env)
        original_targets = control.targets(config)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            template = ROOT / 'monitoring/dashboards/lido-validator-fleet.json'
            control.generate(config, output, template)
            board = json.loads((output / 'dashboards/lido-validator-fleet.json').read_text())
            variable = next(v for v in board['templating']['list'] if v['name'] == 'validator')
            self.assertEqual([v['text'] for v in variable['options']], ['All', 'Apple', 'Bananas', 'Oranges'])
            names = {'validator-1': 'Apple', 'validator-2': 'Bananas', 'validator-3': 'Oranges'}
            activity = next(p for p in board['panels'] if p['id'] == 7)
            for override in activity['fieldConfig']['overrides']:
                ident = override['matcher'].get('options')
                if ident in names:
                    self.assertEqual(next(p['value'] for p in override['properties'] if p['id'] == 'displayName'), names[ident])
            storage = next(p for row in board['panels'] for p in row.get('panels', []) if p['id'] == 36)
            self.assertEqual(storage['transformations'], [])
            for ident, name in names.items():
                matching = [o for o in storage['fieldConfig']['overrides'] if re.search(o['matcher']['options'], ident + ' · /data')]
                self.assertEqual(len(matching), 1)
                display = matching[0]['properties'][0]['value']
                self.assertEqual(display.replace('${__field.labels.mountpoint}', '/data'), name + ' · /data')
            # Changing a display name must not start a new Prometheus series.
            env['VC_NAME'] = 'Green Apple'
            self.assertEqual(control.targets(control.settings(env)), original_targets)
            control.generate(control.settings(env), output, template)
            self.assertNotIn('VC 1', (output / 'dashboards/lido-validator-fleet.json').read_text())

    def test_supervisor_retries_exited_child_and_stops_others(self):
        config = control.settings(environment())
        dead = MagicMock(); dead.poll.return_value = 255; dead.returncode = 255
        alive = MagicMock(); alive.poll.return_value = None
        replacement = MagicMock(); replacement.poll.return_value = None
        callbacks = {}
        ticks = [0]
        def on_signal(number, callback): callbacks[number] = callback
        def sleep(_seconds):
            ticks[0] += 1
            if ticks[0] == 3: callbacks[signal.SIGTERM](signal.SIGTERM, None)
        with patch.object(control.Path, 'is_file', return_value=True), \
             patch.object(control.signal, 'signal', side_effect=on_signal), \
             patch.object(control.time, 'sleep', side_effect=sleep), \
             patch.object(control.time, 'monotonic', side_effect=lambda: ticks[0] * 10), \
             patch.object(control.subprocess, 'Popen', side_effect=[dead, alive, replacement]) as popen, \
             contextlib.redirect_stdout(io.StringIO()):
            control.supervise(config)
        self.assertEqual(popen.call_count, 3)
        alive.terminate.assert_called_once()
        replacement.terminate.assert_called_once()

    def test_live_check_rejects_down_missing_and_wrong_endpoint(self):
        config = control.settings(environment())
        for failure in ('none', 'down', 'missing', 'address', 'path'):
            active = []
            for target in control.targets(config):
                labels = {k: v for k, v in target['labels'].items() if not k.startswith('__')}
                labels.update(job=labels['component'], instance=target['targets'][0])
                active.append({'labels': labels, 'health': 'up', 'lastError': '',
                               'scrapeUrl': 'http://' + target['targets'][0] + target['labels']['__metrics_path__']})
            if failure == 'down': active[0]['health'] = 'down'
            if failure == 'missing': active.pop()
            if failure == 'address': active[0]['labels']['instance'] = 'wrong:9999'
            if failure == 'path': active[0]['scrapeUrl'] = 'http://localhost:8808/wrong'
            def response(data): return io.BytesIO(json.dumps(data).encode())
            opener = MagicMock()
            opener.open.side_effect = [response({'status': 'success', 'data': {'activeTargets': active}}), response({'database': 'ok'})]
            with self.subTest(failure=failure), patch.object(control.urllib.request, 'build_opener', return_value=opener), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(control.check(config), 0 if failure == 'none' else 1)


if __name__ == '__main__':
    unittest.main()
