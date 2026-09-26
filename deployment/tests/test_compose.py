"""Verify deployments using only the two files an operator downloads."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI required')
class StandaloneCompose(unittest.TestCase):
    def render(self, env_text):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            shutil.copyfile(ROOT / 'compose.yml', folder / 'compose.yml')
            (folder / '.env').write_text(env_text)
            # No inherited role selection, Compose paths, credentials or ports.
            env = {key: value for key, value in os.environ.items()
                   if key in ('PATH', 'HOME', 'DOCKER_CONFIG', 'SYSTEMROOT')}
            result = subprocess.run(
                ['docker', 'compose', 'config', '--format', 'json'],
                cwd=folder, env=env, capture_output=True, text=True)
            return result

    def config(self, env_text):
        result = self.render(env_text)
        self.assertEqual(result.returncode, 0, result.stderr)
        model = json.loads(result.stdout)
        for service in model['services'].values():
            self.assertNotIn('build', service)
            self.assertNotIn('ports', service)  # Explicit loopback listeners instead.
        return model

    def test_minimal_child_requires_no_hub_values_or_extra_files(self):
        model = self.config('HUB=false\nCOMPOSE_PROFILES=${HUB:-false}\n')
        self.assertEqual(set(model['services']), {'host-exporter'})
        exporter = model['services']['host-exporter']
        self.assertEqual(exporter['image'], 'prom/node-exporter:v1.12.1')
        self.assertIn('--web.listen-address=127.0.0.1:19103', exporter['command'])

    def test_same_env_switches_roles_and_respects_exporter_port(self):
        template = (ROOT / '.env.example').read_text()
        child = self.config(template.replace('NODE_EXPORTER_PORT=19103', 'NODE_EXPORTER_PORT=19104'))
        self.assertEqual(set(child['services']), {'host-exporter'})
        self.assertIn('--web.listen-address=127.0.0.1:19104', child['services']['host-exporter']['command'])
        hub = self.config(template.replace('\nHUB=false\n', '\nHUB=true\n', 1))
        self.assertEqual(set(hub['services']), {'configure', 'tunnels', 'grafana', 'prometheus', 'host-exporter', 'inventory'})
        for name in ('grafana', 'prometheus', 'tunnels'):
            self.assertEqual(hub['services'][name]['depends_on']['configure']['condition'], 'service_completed_successfully')
        collector = hub['services']['inventory']
        self.assertEqual(collector['command'], ['inventory'])
        self.assertTrue(collector['volumes'][0]['read_only'])
        self.assertEqual(collector['volumes'][0]['target'], '/inventory')
        self.assertNotIn('GRAFANA_ADMIN_PASSWORD', collector['environment'])
        tunnels = hub['services']['tunnels']
        self.assertEqual(tunnels['image'], 'ghcr.io/owlofmoistness/lido-grafana:0.5.0')
        self.assertNotIn('GRAFANA_ADMIN_PASSWORD', tunnels['environment'])
        for mount in tunnels['volumes']:
            self.assertTrue(mount['read_only'])
            # Some Compose versions omit false fields when serializing JSON.
            self.assertFalse(mount.get('bind', {}).get('create_host_path', False))

    def test_local_validator_switch_reaches_all_control_services(self):
        for value in ('true','false'):
            model = self.config('HUB=true\nCOMPOSE_PROFILES=${HUB:-false}\nLOCAL_VALIDATOR_ENABLED='+value+'\n')
            for name in ('configure','tunnels','inventory'):
                with self.subTest(value=value,service=name):
                    self.assertEqual(model['services'][name]['environment']['LOCAL_VALIDATOR_ENABLED'],value)
        default = self.config('HUB=true\nCOMPOSE_PROFILES=${HUB:-false}\n')
        self.assertEqual(default['services']['configure']['environment']['LOCAL_VALIDATOR_ENABLED'],'true')
        child = self.config('HUB=false\nCOMPOSE_PROFILES=${HUB:-false}\nLOCAL_VALIDATOR_ENABLED=false\n')
        self.assertEqual(set(child['services']),{'host-exporter'})

    def test_missing_role_mapping_is_reported(self):
        result = self.render('HUB=true\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('COMPOSE_PROFILES', result.stderr)

    def test_hub_rejects_empty_or_example_password_before_generation(self):
        model = self.config('HUB=true\nCOMPOSE_PROFILES=${HUB:-false}\n')
        # `compose config` escapes dollars for a reusable Compose document.
        script = model['services']['configure']['command'][0].replace('$$', '$')
        for password in ('', 'REPLACE_WITH_A_STRONG_PASSWORD', 'replace-this-before-starting'):
            with self.subTest(password=password):
                result = subprocess.run(['/bin/sh', '-ec', script],
                                        env={'PATH': os.defpath, 'GRAFANA_ADMIN_PASSWORD': password},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('Set a strong GRAFANA_ADMIN_PASSWORD', result.stderr)
                self.assertNotIn('not found', result.stderr)


if __name__ == '__main__':
    unittest.main()
