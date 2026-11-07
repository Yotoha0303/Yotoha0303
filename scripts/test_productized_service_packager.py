#!/usr/bin/env python3
"""Offline acceptance tests.

python3 -m unittest discover -s scripts -p 'test_productized_service_packager.py' -v
"""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import productized_service_packager as packager


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/health")
            self.end_headers()
            return
        status = 200 if self.path in {"/health", "/health?check=1"} else 503
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        status = 201 if self.headers.get("Authorization") == "Bearer client-token" else 401
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "100")
        self.end_headers()


@contextmanager
def http_fixture():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


HTTP_SOURCE = '''from http.server import BaseHTTPRequestHandler, HTTPServer
import argparse
from pathlib import Path
import os
parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, required=True)
args = parser.parse_args()
Path('service.pid').write_text(str(os.getpid()))
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200 if self.path == '/ready' else 404)
        self.end_headers()
        self.wfile.write(b'{"ready":true}')
HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
'''


class PackagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="packager-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "internal project"
        self.project.mkdir()
        self.source = self.write("service.py", "print('client-ready')\n")
        self.output = self.root / "client delivery"

    def write(self, relative, text):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def config(self, **options):
        return replace(packager.PackageConfig(self.source, self.output), **options)

    def bundle(self, **options):
        return packager.build_bundle(self.config(**options))

    def manifest(self):
        return json.loads((self.output / "manifest.json").read_text())

    def run_tool(self, name="smoke_test.py", *args, **options):
        return subprocess.run([sys.executable, str(self.output / name), *args], cwd=self.root,
                              capture_output=True, text=True, timeout=15, **options)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_complete_bundle_has_verified_manifest_and_executable_clients(self):
        result = self.bundle()
        self.assertEqual(result, self.output)
        expected = {"Dockerfile", ".dockerignore", ".gitignore", ".env.example", "README.md",
                    "smoke_test.py", "client_test.py", "delivery_support.py", "test_api.sh",
                    "app/service.py"}
        manifest = self.manifest()
        self.assertEqual(set(manifest["files"]), expected)
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((result / name).read_bytes()).hexdigest(), digest)
        for name in ("smoke_test.py", "client_test.py", "test_api.sh"):
            self.assertEqual((result / name).stat().st_mode & 0o777, 0o755)
        self.assert_ok(self.run_tool())

    def test_bundles_are_byte_identical_and_contain_no_producer_paths(self):
        self.bundle()
        other = packager.build_bundle(self.config(output=self.root / "second"))
        for name in ["manifest.json", *self.manifest()["files"]]:
            self.assertEqual((self.output / name).read_bytes(), (other / name).read_bytes())
            self.assertNotIn(str(self.project).encode(), (other / name).read_bytes())

    def test_packaging_and_static_smoke_never_execute_or_import_production_code(self):
        marker = self.root / "must-not-exist"
        self.write("service.py", f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('executed')\n")
        self.bundle()
        self.assert_ok(self.run_tool())
        self.assertFalse(marker.exists())
        self.assertFalse((self.output / "app/__pycache__").exists())

    def test_detects_recursive_local_imports_cycles_and_explicit_assets(self):
        self.write("service.py", "import helper\nprint(helper.VALUE)\n")
        self.write("helper.py", "import nested\nVALUE = 42\n")
        self.write("nested.py", "import helper\n")
        self.write("private.py", "raise RuntimeError('not selected')\n")
        self.write("assets/sample.json", '{"sample": true}\n')
        self.bundle(include=("assets/sample.json",), smoke_args=("",))
        self.assertEqual({name for name in self.manifest()["files"] if name.startswith("app/")},
                         {"app/service.py", "app/helper.py", "app/nested.py", "app/assets/sample.json"})
        self.assert_ok(self.run_tool("smoke_test.py", "--run"))

    def test_package_relative_imports_and_parent_initializers_run_portably(self):
        source = self.write("pkg/entry.py", "from .sub import helper\nprint(helper.VALUE)\n")
        self.write("pkg/__init__.py", "")
        self.write("pkg/sub/__init__.py", "")
        self.write("pkg/sub/helper.py", "from .. import shared\nVALUE = shared.VALUE\n")
        self.write("pkg/shared.py", "VALUE = 'portable'\n")
        self.bundle(source=source, smoke_args=("",))
        self.assertEqual(self.manifest()["command"], ["python3", "-m", "pkg.entry"])
        self.assertIn("app/pkg/sub/__init__.py", self.manifest()["files"])
        self.assert_ok(self.run_tool("smoke_test.py", "--run"))

    def test_namespace_package_children_are_discovered(self):
        self.write("service.py", "from namespace import helper\nprint(helper.VALUE)\n")
        self.write("namespace/helper.py", "VALUE = 1\n")
        self.bundle(smoke_args=("",))
        self.assertIn("app/namespace/helper.py", self.manifest()["files"])
        self.assert_ok(self.run_tool("smoke_test.py", "--run"))

    def test_environment_detection_aliases_dependencies_and_secret_redaction(self):
        secret = "MUST-NOT-BE-A-TEMPLATE-VALUE"
        self.write("service.py", """import os as system
from os import environ as settings, getenv as lookup
import helper
x = system.getenv('REGION', 'east')
y = settings['REQUIRED_TOKEN']
z = lookup(key='API_KEY', default='hardcoded-source-default')
dynamic = system.getenv(x)
invalid = system.getenv('INVALID=NAME')
""")
        self.write("helper.py", "import os\nx = os.environ.get('HELPER_SECRET')\n")
        with patch.dict(os.environ, {"API_KEY": secret}):
            self.bundle(env=("MANUAL_SETTING", "API_KEY"))
        expected = ["API_KEY", "HELPER_SECRET", "MANUAL_SETTING", "REGION", "REQUIRED_TOKEN"]
        self.assertEqual(self.manifest()["environment"], expected)
        template = (self.output / ".env.example").read_text()
        self.assertEqual([line for line in template.splitlines() if not line.startswith("#")],
                         [name + "=" for name in expected])
        self.assertNotIn(secret, template)
        self.assertNotIn("hardcoded-source-default", template)
        self.assertNotIn("east", template)

    def test_missing_third_party_requirements_fails_before_writing(self):
        self.write("service.py", "import vendor_sdk\n")
        with self.assertRaisesRegex(packager.PackagingError, "require --requirements: vendor_sdk"):
            self.bundle()
        self.assertFalse(self.output.exists())

    def test_requirements_are_copied_and_installed_without_guessing_package_names(self):
        self.write("service.py", "from vendor_sdk import Client\n")
        requirements = self.write("requirements-client.txt", "vendor-distribution==1.2.3\n")
        self.bundle(requirements=requirements)
        self.assertEqual((self.output / "requirements.txt").read_bytes(), requirements.read_bytes())
        self.assertEqual(self.manifest()["external_imports"], ["vendor_sdk"])
        docker = (self.output / "Dockerfile").read_text()
        self.assertIn("python3 -m pip install --no-cache-dir -r requirements.txt", docker)
        self.assertLess(docker.index("pip install"), docker.index("USER 10001:10001"))
        self.assert_ok(self.run_tool())

    def test_nonportable_requirements_paths_urls_options_and_nested_files_are_rejected(self):
        for line in ("-r other.txt", "-e .", "./local", "thing @ https://secret@host/a.whl",
                     "https://host/a.whl", "--extra-index-url https://host", "/tmp/a.whl"):
            with self.subTest(line=line):
                requirements = self.write("requirements.txt", line + "\n")
                with self.assertRaisesRegex(packager.PackagingError, "standalone PyPI"):
                    self.bundle(requirements=requirements)
                self.assertFalse(self.output.exists())

    def test_pep508_style_pypi_ranges_extras_markers_and_comments_are_allowed(self):
        requirements = self.write("requirements.txt", '# selected by producer\nrequests[socks]>=2.32,<3 ; python_version >= "3.10"\n')
        self.bundle(requirements=requirements)
        self.assertTrue(self.manifest()["requirements"])

    def test_existing_outputs_including_empty_directories_are_preserved(self):
        self.output.mkdir()
        with self.assertRaisesRegex(packager.PackagingError, "already exists"):
            self.bundle()
        marker = self.output / "keep.txt"
        marker.write_text("keep")
        with self.assertRaises(packager.PackagingError):
            self.bundle()
        self.assertEqual(marker.read_text(), "keep")

    def test_atomic_write_failure_cleans_staging_without_partial_delivery(self):
        real_write = Path.write_bytes

        def fail(path, content):
            if path.name == "README.md":
                raise OSError("disk full")
            return real_write(path, content)

        with patch.object(Path, "write_bytes", fail):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.bundle()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".delivery-*")))

    def test_missing_source_bad_python_and_unresolved_relative_imports_fail(self):
        for code, message in (("def broken(:\n", "invalid Python"),
                              ("from .missing import run\n", "unresolved relative"),
                              ("from ...outside import run\n", "escapes project_root")):
            with self.subTest(code=code):
                self.write("service.py", code)
                with self.assertRaisesRegex(packager.PackagingError, message):
                    self.bundle()
        with self.assertRaisesRegex(packager.PackagingError, "not a regular file"):
            self.bundle(source=self.project / "missing.py")

    def test_files_outside_project_root_and_traversal_are_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("private")
        for include in ("../outside.txt", str(outside)):
            with self.subTest(include=include):
                with self.assertRaises(packager.PackagingError):
                    self.bundle(include=(include,))
        with self.assertRaises(packager.PackagingError):
            self.bundle(project_root=self.root / "different")

    def test_secrets_generated_files_and_symlinks_are_never_selected(self):
        for name in (".env", ".env.production", "keys/private.key", "cert.pem", ".git/config", "__pycache__/cache.py"):
            self.write(name, "private")
            with self.subTest(name=name):
                with self.assertRaisesRegex(packager.PackagingError, "cannot be bundled"):
                    self.bundle(include=(name,))
        target = self.write("target.py", "print('target')\n")
        link = self.project / "linked.py"
        link.symlink_to(target)
        with self.assertRaisesRegex(packager.PackagingError, "symlinks"):
            self.bundle(include=("linked.py",))
        self.write("service.py", "import linked\n")
        with self.assertRaisesRegex(packager.PackagingError, "symlinks"):
            self.bundle()

    def test_invalid_names_versions_ports_env_and_health_paths_fail_cleanly(self):
        invalid = [{"name": "Bad Name"}, {"name": "../escape"}, {"version": "a\nRUN bad"},
                   {"version": "$(whoami)"}, {"port": 0}, {"port": 65536}, {"port": True},
                   {"env": ("TOKEN=secret",)}, {"env": ("BAD-NAME",)},
                   {"health_path": "//outside/health"}, {"health_path": "/x\nRUN bad"},
                   {"health_path": "/x\0y"}, {"base_image": "python\nRUN bad"},
                   {"args": ("nul\0argument",)}]
        for options in invalid:
            with self.subTest(options=options):
                with self.assertRaises(packager.PackagingError):
                    self.bundle(**options)

    def test_dockerfile_uses_nonroot_json_commands_and_restrictive_build_context(self):
        args = ('--message', 'space " quote; $(echo nope)', '--host', '0.0.0.0')
        self.bundle(args=args, port=8123, health_path="/ready")
        docker = (self.output / "Dockerfile").read_text()
        entrypoint = next(line for line in docker.splitlines() if line.startswith("ENTRYPOINT "))
        cmd = next(line for line in docker.splitlines() if line.startswith("CMD "))
        self.assertEqual(json.loads(entrypoint[11:]), ["python3", "service.py"])
        self.assertEqual(json.loads(cmd[4:]), list(args))
        for line in ("USER 10001:10001", "COPY --chown=10001:10001 app/ ./app/", "EXPOSE 8123"):
            self.assertIn(line, docker)
        self.assertNotIn("COPY .", docker)
        self.assertIn("**/.env", (self.output / ".dockerignore").read_text())
        readme = (self.output / "README.md").read_text()
        self.assertIn("0.0.0.0", readme)
        self.assertIn("-p 127.0.0.1:8123:8123", readme)
        self.assertIn("Rollback".lower(), readme.lower().replace("roll back", "rollback"))

    def test_bash_and_node_runtimes_are_detected_and_extra_env_is_documented(self):
        for suffix, runtime in (("sh", "bash"), ("js", "node"), ("mjs", "node"), ("cjs", "node")):
            with self.subTest(suffix=suffix):
                source = self.write(f"job.{suffix}", "echo okay\n" if suffix == "sh" else "console.log('okay');\n")
                output = self.root / f"bundle-{suffix}"
                packager.build_bundle(self.config(source=source, output=output, env=("EXPLICIT_ENV",)))
                spec = json.loads((output / "manifest.json").read_text())
                self.assertEqual(spec["command"], [runtime, source.name])
                self.assertEqual(spec["environment"], ["EXPLICIT_ENV"])

    def test_custom_runtime_requires_explicit_image_and_one_script_placeholder(self):
        source = self.write("job.rb", "puts 'hello'\n")
        with self.assertRaisesRegex(packager.PackagingError, "unknown runtime"):
            self.bundle(source=source)
        for command in ("ruby {script}", [], ["ruby"], ["ruby", "{script}", "{script}"],
                        ["ruby", 1, "{script}"], 42, {"{script}": "ruby"}):
            with self.subTest(command=command):
                with self.assertRaises(packager.PackagingError):
                    self.bundle(source=source, runtime_command=command, base_image="ruby:3.3-slim")
        self.bundle(source=source, runtime_command=("ruby", "{script}"), base_image="ruby:3.3-slim")
        self.assertEqual(self.manifest()["command"], ["ruby", "job.rb"])

    def test_cli_smoke_execution_succeeds_and_smoke_args_replace_normal_args(self):
        self.write("service.py", "import sys\nassert sys.argv[1:] == ['--offline', 'two words']\n")
        self.bundle(args=("--live",), smoke_args=("--offline", "two words"))
        result = self.run_tool("smoke_test.py", "--run")
        self.assert_ok(result)
        self.assertIn("execution verified", result.stdout)

    def test_empty_smoke_argument_is_preserved_inside_an_argument_list(self):
        self.write("service.py", "import sys\nassert sys.argv[1:] == ['--value', '']\n")
        self.bundle(smoke_args=("--value", ""))
        self.assertEqual(self.manifest()["smoke_args"], ["--value", ""])
        self.assert_ok(self.run_tool("smoke_test.py", "--run"))

    def test_cli_smoke_requires_explicit_safe_arguments(self):
        self.bundle()
        result = self.run_tool("smoke_test.py", "--run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires packaging with --smoke-arg", result.stderr)

    def test_cli_smoke_reports_nonzero_exit(self):
        self.write("service.py", "raise SystemExit(7)\n")
        self.bundle(smoke_args=("",))
        result = self.run_tool("smoke_test.py", "--run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("exit 7", result.stderr)

    def test_cli_smoke_timeout_terminates_the_process(self):
        self.write("service.py", "import os, time\nfrom pathlib import Path\nPath('process.pid').write_text(str(os.getpid()))\ntime.sleep(30)\n")
        self.bundle(smoke_args=("",))
        result = self.run_tool("smoke_test.py", "--run", "--timeout", "0.5")
        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out", result.stderr)
        pid = int((self.output / "app/process.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_smoke_detects_tampered_and_missing_files(self):
        self.bundle()
        source = self.output / "app/service.py"
        source.write_text("raise SystemExit(0)\n")
        result = self.run_tool()
        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum mismatch: app/service.py", result.stderr)
        source.unlink()
        self.assertNotEqual(self.run_tool().returncode, 0)

    def test_smoke_rejects_incomplete_manifest_and_path_traversal(self):
        self.bundle()
        spec = self.manifest()
        for change in (lambda s: s["files"].pop("Dockerfile"),
                       lambda s: s["files"].update({"../outside.txt": "0" * 64})):
            original = json.loads(json.dumps(spec))
            change(original)
            (self.output / "manifest.json").write_text(json.dumps(original))
            result = self.run_tool()
            self.assertNotEqual(result.returncode, 0)

    def test_http_smoke_launches_checks_health_and_cleans_up(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.write("service.py", HTTP_SOURCE)
        self.bundle(port=port, health_path="/ready", args=("--port", str(port)))
        result = self.run_tool("smoke_test.py", "--run", "--timeout", "3")
        self.assert_ok(result)
        pid = int((self.output / "app/service.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with socket.socket() as connection:
            self.assertNotEqual(connection.connect_ex(("127.0.0.1", port)), 0)

    def test_http_smoke_can_probe_existing_deployment_and_fails_unhealthy_endpoint(self):
        self.bundle()
        with http_fixture() as url:
            self.assert_ok(self.run_tool("smoke_test.py", "--url", url, "--timeout", "1"))
            result = self.run_tool("smoke_test.py", "--url", url + "/unhealthy", "--timeout", "0.2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("HTTP 503", result.stderr)

    def test_http_smoke_fails_if_launched_service_exits_before_ready(self):
        self.write("service.py", "raise SystemExit(9)\n")
        self.bundle(port=1)
        result = self.run_tool("smoke_test.py", "--run", "--timeout", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("exited before becoming healthy", result.stderr)

    def test_http_smoke_never_mistakes_an_existing_listener_for_launched_service(self):
        with http_fixture() as url:
            port = int(url.rsplit(":", 1)[1])
            self.write("service.py", "raise SystemExit(9)\n")
            self.bundle(port=port)
            result = self.run_tool("smoke_test.py", "--run", "--timeout", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already in use", result.stderr)

    def test_python_http_client_success_failure_post_body_and_bearer_auth(self):
        self.bundle()
        with http_fixture() as url:
            self.assert_ok(self.run_tool("client_test.py", "--url", url, "--non-interactive"))
            result = self.run_tool("client_test.py", "--url", url, "--path", "/unhealthy", "--non-interactive")
            self.assertEqual(result.returncode, 1)
            self.assertIn("HTTP 503", result.stdout)
            result = self.run_tool("client_test.py", "--url", url, "--path", "/echo", "--method", "POST",
                                   "--data", '{"client":"测试"}', "--expect-status", "201", "--non-interactive",
                                   env={**os.environ, "DELIVERY_BEARER_TOKEN": "client-token"})
            self.assert_ok(result)
            self.assertIn('"client":"测试"', result.stdout)
            self.assertNotIn("client-token", result.stdout + result.stderr)

    def test_python_http_client_does_not_follow_redirects(self):
        self.bundle()
        with http_fixture() as url:
            result = self.run_tool("client_test.py", "--url", url, "--path", "/redirect", "--non-interactive")
        self.assertEqual(result.returncode, 1)
        self.assertIn("HTTP 302", result.stdout)

    def test_python_http_client_rejects_unsafe_urls_paths_and_invalid_json(self):
        self.bundle()
        invalid = [("--url", "file:///etc/passwd"), ("--url", "http://secret@localhost"),
                   ("--url", "http://localhost/#fragment"), ("--path", "//host"),
                   ("--data", "{invalid}"), ("--timeout", "nan"), ("--timeout", "0"),
                   ("--timeout", "inf"), ("--expect-status", "999")]
        for args in invalid:
            with self.subTest(args=args):
                result = self.run_tool("client_test.py", "--non-interactive", *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("curl"), "Bash/curl not installed")
    def test_generated_curl_client_real_get_post_and_failure_exit_codes(self):
        self.bundle()
        syntax = subprocess.run(["bash", "-n", str(self.output / "test_api.sh")], capture_output=True, text=True)
        self.assert_ok(syntax)
        with http_fixture() as url:
            environment = {**os.environ, "BASE_URL": url}
            result = subprocess.run(["bash", str(self.output / "test_api.sh")], input="", capture_output=True,
                                    text=True, timeout=5, env=environment)
            self.assert_ok(result)
            environment.update(HTTP_METHOD="POST", HTTP_PATH="/echo", HTTP_BODY='{"sample":true}',
                               HTTP_EXPECT_STATUS="201", DELIVERY_BEARER_TOKEN="client-token")
            result = subprocess.run(["bash", str(self.output / "test_api.sh")], input="", capture_output=True,
                                    text=True, timeout=5, env=environment)
            self.assert_ok(result)
            self.assertIn('{"sample":true}', result.stdout)
            environment["HTTP_EXPECT_STATUS"] = "200"
            result = subprocess.run(["bash", str(self.output / "test_api.sh")], input="", capture_output=True,
                                    text=True, timeout=5, env=environment)
            self.assertNotEqual(result.returncode, 0)
            environment.update(HTTP_METHOD="HEAD", HTTP_PATH="/health", HTTP_BODY="")
            result = subprocess.run(["bash", str(self.output / "test_api.sh")], input="", capture_output=True,
                                    text=True, timeout=5, env=environment)
            self.assert_ok(result)
            environment["HTTP_TIMEOUT"] = "0"
            result = subprocess.run(["bash", str(self.output / "test_api.sh")], input="", capture_output=True,
                                    text=True, timeout=5, env=environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("positive number", result.stderr)

    def test_generated_client_interactive_prompts_accept_defaults(self):
        self.bundle()
        sys.path.insert(0, str(self.output))
        self.addCleanup(lambda: sys.path.remove(str(self.output)))
        import importlib.util
        spec = importlib.util.spec_from_file_location("generated_client", self.output / "client_test.py")
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        self.addCleanup(lambda: sys.modules.pop("delivery_support", None))
        with http_fixture() as url, patch.object(sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=["", "", ""]), redirect_stdout(io.StringIO()):
            self.assertEqual(client.main(["--url", url]), 0)

    def test_cli_entrypoint_generates_bundle_and_reports_errors_without_traceback(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = packager.main([str(self.source), "--output", str(self.output), "--name", "client-service",
                                    "--description", "Client-specific delivery", "--smoke-arg=--help"])
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertIn("Client delivery ready:", stdout.getvalue())
        self.assertEqual(self.manifest()["smoke_args"], ["--help"])
        self.assertIn("Client-specific delivery", (self.output / "README.md").read_text())
        with redirect_stderr(stderr):
            result = packager.main([str(self.source), "--output", str(self.output)])
        self.assertEqual(result, 1)
        self.assertIn("already exists", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_repository_production_script_is_packaged_and_runs_offline(self):
        source = Path(__file__).resolve().parent / "sovereign_yield_harvester.py"
        self.bundle(source=source, smoke_args=("--offline", "--output", "result.json"))
        result = self.run_tool("smoke_test.py", "--run", "--timeout", "5")
        self.assert_ok(result)
        payload = json.loads((self.output / "app/result.json").read_text())
        self.assertGreater(payload["record_count"], 0)


if __name__ == "__main__":
    unittest.main()
