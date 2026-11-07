#!/usr/bin/env python3
"""Build portable client deliveries without importing or running production code.

    python3 scripts/productized_service_packager.py scripts/sovereign_yield_harvester.py \
        --output /tmp/yield-delivery --smoke-arg=--offline
    python3 /tmp/yield-delivery/smoke_test.py --run

Python, Bash and Node entrypoints are detected automatically. Other runtimes use
--runtime-command '["ruby", "{script}"]' --base-image ruby:3.3-slim. The engine
itself and its generated Python tools use only the standard library (Python 3.10+).
Static Python imports and literal environment names are discovered recursively;
dynamic imports, runtime data, system libraries and dependencies require explicit
--include, --requirements or --base-image options. Existing outputs are never
overwritten. No script is executed during packaging.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
import tokenize


SCHEMA_VERSION = "1.0"
RUNTIMES = {
    ".py": ("python:3.12-slim", ("python3", "{script}")),
    ".sh": ("debian:bookworm-slim", ("bash", "{script}")),
    ".js": ("node:22-bookworm-slim", ("node", "{script}")),
    ".mjs": ("node:22-bookworm-slim", ("node", "{script}")),
    ".cjs": ("node:22-bookworm-slim", ("node", "{script}")),
}
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
IMAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*\Z")
BLOCKED_PARTS = {".git", ".venv", "__pycache__", "node_modules"}


class PackagingError(ValueError):
    """An input cannot produce a portable delivery."""


@dataclass(frozen=True)
class PackageConfig:
    source: Path
    output: Path
    name: str | None = None
    version: str = "1.0.0"
    description: str = "A packaged production service."
    project_root: Path | None = None
    include: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    requirements: Path | None = None
    runtime_command: tuple[str, ...] | None = None
    base_image: str | None = None
    args: tuple[str, ...] = ()
    smoke_args: tuple[str, ...] | None = None
    port: int | None = None
    health_path: str = "/health"


@dataclass
class SourceAnalysis:
    files: dict[str, bytes] = field(default_factory=dict)
    environment: set[str] = field(default_factory=set)
    external_imports: set[str] = field(default_factory=set)


def _read_file(path: Path, root: Path) -> tuple[str, bytes]:
    # Resolve only after checking every component: symlinks must never silently
    # pull an unrelated file (including a developer's secrets) into a delivery.
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise PackagingError("source and included files must be inside project_root") from None
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise PackagingError("file paths may not contain traversal components")
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise PackagingError(f"symlinks are not supported: {relative}")
    if (any(part in BLOCKED_PARTS for part in relative.parts)
            or any(part.startswith(".env") and part != ".env.example" for part in relative.parts)
            or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}):
        raise PackagingError(f"private or generated file cannot be bundled: {relative}")
    if not path.is_file():
        raise PackagingError(f"not a regular file: {relative}")
    if any(ord(char) < 32 or char in "\\`" for char in relative.as_posix()):
        raise PackagingError("file names may not contain control characters, backslashes or backticks")
    return relative.as_posix(), path.read_bytes()


def _python_tree(path: Path) -> ast.AST:
    try:
        with tokenize.open(path) as handle:
            return ast.parse(handle.read(), filename=path.name)
    except (SyntaxError, UnicodeError, LookupError) as exc:
        raise PackagingError(f"invalid Python source {path.name}: {exc}") from None


def discover_environment(tree: ast.AST) -> set[str]:
    """Find literal os.getenv/environ references, including imported aliases.

    Values and defaults are deliberately never extracted or evaluated.
    """
    aliases = {"os": "os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "os":
                    aliases[item.asname or item.name] = "os"
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for item in node.names:
                if item.name in {"environ", "getenv"}:
                    aliases[item.asname or item.name] = "os." + item.name

    def dotted(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return dotted(node.value) + "." + node.attr
        return ""

    names = set()
    for node in ast.walk(tree):
        key = None
        if isinstance(node, ast.Subscript) and dotted(node.value) == "os.environ":
            key = node.slice
        elif isinstance(node, ast.Call) and dotted(node.func) in {
                "os.getenv", "os.environ.get", "os.environ.setdefault"}:
            key = node.args[0] if node.args else next(
                (item.value for item in node.keywords if item.arg == "key"), None)
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and ENV_NAME.fullmatch(key.value):
            names.add(key.value)
    return names


def analyze_source(source: Path, root: Path, includes=()) -> SourceAnalysis:
    """Copy only selected files and reachable local Python modules, cycle safely."""
    analysis = SourceAnalysis()
    pending = [source] + [root / item for item in includes]

    def module_path(base, module):
        candidate = base.joinpath(*module.split(".")) if module else base
        for option in (candidate.with_suffix(".py"), candidate / "__init__.py") if module else (
                candidate / "__init__.py",):
            if option.is_file() or option.is_symlink():
                return option
        return None

    def queue_parents(path):
        for parent in path.parents:
            if parent == root or root not in parent.parents:
                break
            init = parent / "__init__.py"
            if init.is_file():
                pending.append(init)

    while pending:
        path = pending.pop()
        relative, content = _read_file(path, root)
        if relative in analysis.files:
            continue
        analysis.files[relative] = content
        queue_parents(path)
        if path.suffix.lower() != ".py":
            continue
        tree = _python_tree(path)
        analysis.environment.update(discover_environment(tree))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [(item.name, 0, ()) for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [(node.module or "", node.level, node.names)]
            else:
                continue
            for module, level, children in imports:
                if level:
                    base = path.parent
                    for _ in range(level - 1):
                        base = base.parent
                    if base != root and root not in base.parents:
                        raise PackagingError(f"relative import escapes project_root: {relative}")
                    bases = [base]
                else:
                    bases = list(dict.fromkeys([root, path.parent]))
                found = next((result for base in bases
                              if (result := module_path(base, module)) is not None), None)
                # Namespace packages have no __init__.py; still collect their
                # explicitly imported children (from namespace import module).
                child_found = False
                for child in children:
                    if child.name == "*":
                        continue
                    child_name = ".".join(filter(None, (module, child.name)))
                    for base in bases:
                        result = module_path(base, child_name)
                        if result is not None:
                            pending.append(result)
                            child_found = True
                            break
                if found is not None:
                    pending.append(found)
                elif not child_found:
                    if level:
                        raise PackagingError(f"unresolved relative import in {relative}: {module}")
                    top = module.split(".")[0]
                    if top and top not in sys.stdlib_module_names and top != "__future__":
                        analysis.external_imports.add(top)
    return analysis


SUPPORT_TEMPLATE = r'''"""Shared standard-library helpers for this client delivery."""
import json
import math
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent


def manifest():
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


def positive_timeout(value):
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 300:
        raise ValueError("timeout must be greater than 0 and at most 300 seconds")
    return value


def endpoint(base, path):
    parts = urlsplit(base)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or any(ord(c) < 33 or c.isspace() for c in base)):
        raise ValueError("base URL must be HTTP(S), without credentials, query or fragment")
    if (not path.startswith("/") or path.startswith("//")
            or any(ord(c) < 33 or c.isspace() for c in path) or "#" in path):
        raise ValueError("request path must start with one slash and contain no whitespace or fragment")
    return base.rstrip("/") + path


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, *, method="GET", data=None, token="", timeout=10):
    headers = {"Accept": "application/json"}
    if "\r" in token or "\n" in token:
        raise ValueError("Bearer token may not contain newlines")
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=None if data is None else data.encode("utf-8"),
                  headers=headers, method=method)
    try:
        response = build_opener(NoRedirect).open(req, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        # Bound output even if a remote server streams an unexpectedly large body.
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("HTTP response exceeds 1 MiB")
        return response.status, body.decode("utf-8", errors="replace")
'''

CLIENT_TEMPLATE = r'''#!/usr/bin/env python3
"""Interactive HTTP client. Use --non-interactive in CI; tokens come from the environment."""
import argparse
import os
import sys
from delivery_support import endpoint, manifest, positive_timeout, request


def main(argv=None):
    spec = manifest()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("BASE_URL", spec["http"]["base_url"]))
    parser.add_argument("--path", default=spec["http"]["health_path"])
    parser.add_argument("--method", choices=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], default="GET")
    parser.add_argument("--data", help="JSON request body")
    parser.add_argument("--expect-status", type=int, default=200)
    parser.add_argument("--timeout", type=positive_timeout, default=10)
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.non_interactive and sys.stdin.isatty():
            args.url = input("Base URL [" + args.url + "]: ").strip() or args.url
            args.path = input("Request path [" + args.path + "]: ").strip() or args.path
            args.method = input("Method [" + args.method + "]: ").strip().upper() or args.method
            if args.method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                raise ValueError("unsupported HTTP method")
            if args.method in {"POST", "PUT", "PATCH"} and args.data is None:
                args.data = input("JSON body (blank for none): ") or None
        if not 100 <= args.expect_status <= 599:
            raise ValueError("expected status must be in 100..599")
        if args.data is not None:
            import json
            json.loads(args.data)
        status, body = request(endpoint(args.url, args.path), method=args.method, data=args.data,
                               token=os.environ.get("DELIVERY_BEARER_TOKEN", ""), timeout=args.timeout)
        print("HTTP", status)
        print(body)
        return 0 if status == args.expect_status else 1
    except (OSError, ValueError, EOFError) as exc:
        print("HTTP test failed:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

SMOKE_TEMPLATE = r'''#!/usr/bin/env python3
"""Verify bundle contents; --run explicitly executes the supplied production code."""
import argparse
import hashlib
import os
from pathlib import PurePosixPath
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from delivery_support import ROOT, endpoint, manifest, positive_timeout, request


def verify(spec):
    required = {"Dockerfile", ".env.example", "README.md", "client_test.py", "test_api.sh",
                "smoke_test.py", "delivery_support.py", ".dockerignore", ".gitignore"}
    if spec["schema_version"] != "1.0" or not required <= spec["files"].keys():
        raise ValueError("incomplete or unsupported manifest")
    for name, digest in spec["files"].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise ValueError("unsafe manifest path")
        path = ROOT / name
        if any(p.is_symlink() for p in [path, *path.parents] if p != ROOT.parent):
            raise ValueError("symlink in bundle: " + name)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("checksum mismatch: " + name)
        if path.suffix.lower() == ".py":
            compile(content, name, "exec")
    print("Verified", len(spec["files"]), "files and Python syntax")


def wait_http(url, timeout, process=None):
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise ValueError("service exited before becoming healthy (exit " + str(process.returncode) + ")")
        try:
            status, _ = request(url, token=os.environ.get("DELIVERY_BEARER_TOKEN", ""),
                                timeout=min(1, max(0.01, deadline - time.monotonic())))
            if status == 200:
                return
            last = "HTTP " + str(status)
        except (OSError, ValueError) as exc:
            last = str(exc)
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    raise ValueError("health check timed out: " + last)


def stop(process):
    # A separate group also allows cleanup of workers spawned by the entrypoint.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
    finally:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="launch and test the packaged entrypoint")
    parser.add_argument("--url", help="probe an already-running HTTP deployment, or override the launched service URL")
    parser.add_argument("--timeout", type=positive_timeout, default=10)
    args = parser.parse_args(argv)
    process = None
    try:
        spec = manifest()
        verify(spec)
        url = None
        if args.url or (args.run and spec["http"]["enabled"]):
            url = endpoint(args.url or spec["http"]["base_url"], spec["http"]["health_path"])
        if args.run:
            smoke_args = spec["smoke_args"]
            if not spec["http"]["enabled"] and smoke_args is None:
                raise ValueError("CLI execution requires packaging with --smoke-arg (use --smoke-arg='' for no arguments)")
            command = spec["command"] + (spec["args"] if smoke_args is None else smoke_args)
            if url:
                address = urlsplit(url)
                try:
                    connection = socket.create_connection(
                        (address.hostname, address.port or (443 if address.scheme == "https" else 80)),
                        timeout=min(0.25, args.timeout))
                except OSError:
                    pass
                else:
                    connection.close()
                    raise ValueError("HTTP address is already in use; probe it with --url without --run")
            with tempfile.TemporaryFile() as logs:
                process = subprocess.Popen(command, cwd=ROOT / "app", stdout=logs, stderr=logs,
                                           start_new_session=(os.name == "posix"))
                try:
                    if url:
                        wait_http(url, args.timeout, process)
                    else:
                        try:
                            code = process.wait(timeout=args.timeout)
                        except subprocess.TimeoutExpired:
                            raise ValueError("CLI smoke command timed out") from None
                        if code != 0:
                            raise ValueError("CLI smoke command failed (exit " + str(code) + ")")
                finally:
                    stop(process)
        elif url:
            wait_http(url, args.timeout)
        print("Smoke test passed" + (" (execution verified)" if args.run else ""))
        return 0
    except (OSError, ValueError, SyntaxError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print("Smoke test failed:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _curl_script(base_url: str, health_path: str) -> str:
    return "#!/usr/bin/env bash\nset -euo pipefail\n" + (
        "BASE_URL=${BASE_URL-" + shlex.quote(base_url) + "}\n"
        "HTTP_PATH=${HTTP_PATH-" + shlex.quote(health_path) + "}\n") + r'''
HTTP_METHOD=${HTTP_METHOD:-GET}
HTTP_EXPECT_STATUS=${HTTP_EXPECT_STATUS:-200}
HTTP_TIMEOUT=${HTTP_TIMEOUT:-10}
HTTP_BODY=${HTTP_BODY-}
if [[ -t 0 ]]; then
  read -r -p "Base URL [$BASE_URL]: " answer
  BASE_URL=${answer:-$BASE_URL}
  read -r -p "Request path [$HTTP_PATH]: " answer
  HTTP_PATH=${answer:-$HTTP_PATH}
  read -r -p "Method [$HTTP_METHOD]: " answer
  HTTP_METHOD=${answer:-$HTTP_METHOD}
  if [[ $HTTP_METHOD == POST || $HTTP_METHOD == PUT || $HTTP_METHOD == PATCH ]]; then
    read -r -p "JSON body [$HTTP_BODY]: " answer
    HTTP_BODY=${answer:-$HTTP_BODY}
  fi
fi
case "$BASE_URL" in http://*|https://*) ;; *) echo 'Set BASE_URL to an HTTP(S) deployment URL.' >&2; exit 1;; esac
case "$HTTP_PATH" in //*) echo 'Path must start with one slash.' >&2; exit 1;; /*) ;; *) exit 1;; esac
case "$HTTP_METHOD" in GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) ;; *) echo 'Unsupported method.' >&2; exit 1;; esac
if [[ ! $HTTP_TIMEOUT =~ ^[0-9]+([.][0-9]+)?$ || ! $HTTP_TIMEOUT =~ [1-9] ]]; then
  echo 'HTTP_TIMEOUT must be a positive number of seconds.' >&2
  exit 1
fi
command -v curl >/dev/null || { echo 'Install curl to run this client.' >&2; exit 1; }
body_file=$(mktemp)
trap 'rm -f "$body_file"' EXIT
options=(--silent --show-error --max-time "$HTTP_TIMEOUT" --request "$HTTP_METHOD"
         --output "$body_file" --write-out '%{http_code}' --header 'Accept: application/json')
if [[ $HTTP_METHOD == HEAD ]]; then
  options+=(--head)
fi
if [[ -n ${DELIVERY_BEARER_TOKEN-} ]]; then
  options+=(--header "Authorization: Bearer $DELIVERY_BEARER_TOKEN")
fi
if [[ -n $HTTP_BODY ]]; then
  options+=(--header 'Content-Type: application/json' --data-raw "$HTTP_BODY")
fi
status=$(curl "${options[@]}" -- "${BASE_URL%/}$HTTP_PATH")
printf 'HTTP %s\n' "$status"
cat "$body_file"
printf '\n'
[[ $status == "$HTTP_EXPECT_STATUS" ]]
'''


def _readme(spec: dict, description: str) -> str:
    name = spec["name"]
    command = shlex.join(spec["command"] + spec["args"])
    http = spec["http"]
    mapping = f" -p 127.0.0.1:{http['port']}:{http['port']}" if http["enabled"] else ""
    environment = ", ".join(f"`{key}`" for key in spec["environment"]) or "None detected."
    imports = ", ".join(spec["external_imports"]) or "None detected."
    http_notes = (f"The service is configured for port {http['port']} and health path `{http['health_path']}`. "
                  "Its launch arguments/environment must bind to 0.0.0.0 inside Docker; the packager does not change application code."
                  if http["enabled"] else
                  "This is a CLI delivery. HTTP clients require BASE_URL or --url pointing to an HTTP deployment of the service.")
    return f"""# {name}

{description}

Delivery version: {spec['version']}. Runtime image: `{spec['base_image']}`.

## Quick start

1. Install Python 3.10+ for the delivery tools and Docker for container execution.
2. Copy `.env.example` to `.env` and fill in the variables needed by the application.
   Every detected value is blank, including defaults; no local environment values are copied.
   Omit optional variables to retain the application's own defaults. Do not commit `.env`.
3. Verify the delivery, build, and run from this directory:

```sh
python3 smoke_test.py
cp .env.example .env
# Edit .env before starting the service.
docker build -t {name}:{spec['version']} .
docker run --rm --env-file .env{mapping} {name}:{spec['version']}
```

The container runs as UID/GID 10001 in a writable `/opt/service/app` directory.
Persist application output using a writable volume; do not rely on container storage.
Pass arguments after the image name to replace the default arguments. Stop with Ctrl+C
or `docker stop <container>`. Roll back by running the previous versioned image.

## Run locally

Install the entrypoint's runtime separately. {('Install dependencies with `python3 -m pip install -r requirements.txt`.' if spec['requirements'] else 'No Python requirements file was supplied.')}
Export the required environment variables in your shell; these tools do not load `.env` automatically.
Then run:

```sh
cd app
{command}
```

## Client acceptance tests

`python3 smoke_test.py` verifies SHA-256 checksums of every delivered file and compiles
all Python sources without importing or executing them. The manifest itself is not signed;
checksums detect accidental changes, not malicious replacement of the delivery.

`python3 smoke_test.py --run --timeout 10` explicitly launches production code.
For CLI services this requires configured smoke arguments and checks for exit status 0.
For HTTP services it waits for HTTP 200 at the health endpoint and stops the service
and its process group afterward. Smoke arguments replace normal arguments when provided.
`--url http://127.0.0.1:PORT` probes an existing deployment without launching a process.
Verification alone does not prove dependencies, external tools, credentials or business behavior.

{http_notes}

```sh
python3 client_test.py                   # interactive HTTP client
bash test_api.sh                         # interactive cURL client (requires Bash/curl)
python3 client_test.py --non-interactive --url http://127.0.0.1:8080
```

For a JSON request use `client_test.py --method POST --path /your-endpoint --data '{{"sample":true}}'`.
Set `DELIVERY_BEARER_TOKEN` in the environment for authentication. The cURL client accepts
`BASE_URL`, `HTTP_PATH`, `HTTP_METHOD`, `HTTP_BODY`, `HTTP_EXPECT_STATUS` and `HTTP_TIMEOUT`.
Both clients fail on an unexpected status (default 200); redirects are not followed.
Python client responses are limited to 1 MiB. All probes use finite timeouts.

## Configuration and handover

Detected/declared environment variables: {environment}

Detected third-party Python import roots: {imports}

`manifest.json` records the entrypoint, launch/smoke arguments, environment names,
runtime, and file hashes. `app/` contains only the script, reachable static local
Python imports and explicitly selected files. Python import discovery is conservative:
optional imports are included, while dynamic imports, plugins, non-Python imports,
runtime data files and system binaries need explicit selection and review. Requirements
are supplied by the producer; import names are never guessed as pip distribution names.
Requirements use standalone PyPI specifications; local paths, URLs and nested files
are rejected. Use an appropriate base image for native libraries or other runtimes.
The Docker build may download its base image and declared Python dependencies.

Before handover, review selected source files and application output for credentials,
confirm licensing and deployment configuration, fill in client-specific inputs,
and run an execution smoke test with safe representative arguments. Packaging never
executes production code and cannot remove hardcoded credentials from that code.
"""


def _validate(config: PackageConfig):
    source = Path(os.path.abspath(config.source))
    if config.project_root is None:
        root = source.parent
        while (root / "__init__.py").is_file():
            root = root.parent
    else:
        root = Path(config.project_root).resolve()
    if root.is_symlink() or not root.is_dir():
        raise PackagingError("project_root must be a directory")
    _read_file(source, root)
    name = config.name or re.sub(r"[^a-z0-9]+", "-", source.stem.lower()).strip("-")
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        raise PackagingError("name must be 1..63 lowercase letters, digits or hyphens")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", config.version):
        raise PackagingError("version must be a valid Docker tag")
    if not isinstance(config.description, str) or not config.description.strip():
        raise PackagingError("description must be nonempty text")
    if config.port is not None and (type(config.port) is not int or not 1 <= config.port <= 65535):
        raise PackagingError("port must be in 1..65535")
    if (not config.health_path.startswith("/") or config.health_path.startswith("//")
            or any(ord(char) < 33 or char.isspace() for char in config.health_path) or "#" in config.health_path):
        raise PackagingError("health_path must be a URL path starting with one slash")
    if any(not isinstance(key, str) or not ENV_NAME.fullmatch(key) for key in config.env):
        raise PackagingError("environment entries must be variable names, without values")
    for arguments in (config.args, config.smoke_args or ()):
        if isinstance(arguments, str) or any(not isinstance(item, str) or "\0" in item for item in arguments):
            raise PackagingError("arguments must be strings without NUL characters")
    default = RUNTIMES.get(source.suffix.lower())
    if config.runtime_command is None and default is None:
        raise PackagingError("unknown runtime; supply --runtime-command and --base-image")
    command = config.runtime_command if config.runtime_command is not None else default[1]
    image = config.base_image or (default[0] if default else None)
    if (not image or not IMAGE_NAME.fullmatch(image) or image.startswith("-")):
        raise PackagingError("base_image must be a valid image reference")
    if (not isinstance(command, (list, tuple)) or not command or not all(isinstance(item, str) and item
            and not any(ord(c) < 32 for c in item) for item in command)
            or sum(item.count("{script}") for item in command) != 1):
        raise PackagingError("runtime_command must be a string array containing exactly one {script}")
    return source, root, name, image, command


def _requirements(path: Path) -> bytes:
    content = path.read_bytes()
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError:
        raise PackagingError("requirements must be UTF-8") from None
    # A delivery must not depend on the producer's filesystem or leak URL credentials.
    for line in lines:
        requirement = line.split("#", 1)[0].strip()
        if requirement and (not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(\[[A-Za-z0-9_,.-]+\])?(\s*[<>=!~;].*)?$", requirement)
                            or any(char in requirement for char in ("/", "\\", "@", "\0"))):
            raise PackagingError("requirements must contain standalone PyPI specifications, not paths, URLs or options")
    return content


def build_bundle(config: PackageConfig) -> Path:
    """Create a new delivery atomically. Return its absolute directory path."""
    source, root, name, image, runtime = _validate(config)
    output = Path(os.path.abspath(config.output))
    if output.exists() or output.is_symlink():
        raise PackagingError("output already exists; choose a new delivery directory")
    analysis = analyze_source(source, root, config.include)
    requirements = _requirements(Path(config.requirements)) if config.requirements else None
    if analysis.external_imports and requirements is None:
        raise PackagingError("third-party imports require --requirements: " + ", ".join(sorted(analysis.external_imports)))
    if requirements is not None and source.suffix.lower() != ".py":
        raise PackagingError("--requirements is supported only for Python entrypoints")
    relative = source.relative_to(root).as_posix()
    command = [item.replace("{script}", relative) for item in runtime]
    if source.suffix.lower() == ".py" and config.runtime_command is None:
        parts = source.relative_to(root).parts
        if len(parts) > 1 and all((root.joinpath(*parts[:i]) / "__init__.py").is_file()
                                  for i in range(1, len(parts))):
            command = ["python3", "-m", ".".join((*parts[:-1], source.stem))]
    # Empty --smoke-arg explicitly authorizes a zero-argument CLI smoke run.
    smoke_args = None if config.smoke_args is None else list(config.smoke_args)
    if smoke_args == [""]:
        smoke_args = []
    spec = {
        "schema_version": SCHEMA_VERSION, "name": name, "version": config.version,
        "entrypoint": "app/" + relative, "base_image": image, "command": command,
        "args": list(config.args), "smoke_args": smoke_args,
        "environment": sorted(analysis.environment | set(config.env)),
        "external_imports": sorted(analysis.external_imports), "requirements": requirements is not None,
        "http": {"enabled": config.port is not None, "port": config.port,
                 "health_path": config.health_path,
                 "base_url": f"http://127.0.0.1:{config.port}" if config.port else ""},
    }
    files = {"app/" + key: value for key, value in analysis.files.items()}
    docker = [f"FROM {image}", "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1",
              "WORKDIR /opt/service"]
    if requirements is not None:
        files["requirements.txt"] = requirements
        docker += ["COPY requirements.txt ./requirements.txt",
                   "RUN python3 -m pip install --no-cache-dir -r requirements.txt"]
    docker += ["COPY --chown=10001:10001 app/ ./app/", "USER 10001:10001", "WORKDIR /opt/service/app"]
    if config.port:
        docker.append(f"EXPOSE {config.port}")
    docker += ["ENTRYPOINT " + json.dumps(command, ensure_ascii=True), "CMD " + json.dumps(list(config.args), ensure_ascii=True)]
    environment = "# Fill in required values; omit optional entries to retain application defaults.\n"
    environment += "# Values are never copied from source defaults or the producer's environment.\n"
    environment += "".join(f"{key}=\n" for key in spec["environment"])
    text_files = {
        "Dockerfile": "\n".join(docker) + "\n", ".env.example": environment,
        ".dockerignore": "**\n!Dockerfile\n!app/\n!app/**\n!requirements.txt\n**/.env\n**/.env.*\n**/__pycache__\n**/*.pyc\n",
        ".gitignore": ".env\n.env.*\n!.env.example\n__pycache__/\n*.pyc\n",
        "README.md": _readme(spec, config.description.strip()),
        "delivery_support.py": SUPPORT_TEMPLATE, "client_test.py": CLIENT_TEMPLATE,
        "smoke_test.py": SMOKE_TEMPLATE,
        "test_api.sh": _curl_script(spec["http"]["base_url"], config.health_path),
    }
    files.update({key: value.encode("utf-8") for key, value in text_files.items()})
    spec["files"] = {key: hashlib.sha256(value).hexdigest() for key, value in sorted(files.items())}
    files["manifest.json"] = (json.dumps(spec, indent=2, ensure_ascii=True, sort_keys=True) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".delivery-", dir=output.parent))
    try:
        for relative, content in sorted(files.items()):
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            destination.chmod(0o755 if relative in {"smoke_test.py", "client_test.py", "test_api.sh"} else 0o644)
        # rename cannot overwrite a nonempty directory. Explicitly refuse empty
        # directories too, including one created while the bundle was rendering.
        if output.exists() or output.is_symlink():
            raise PackagingError("output already exists; choose a new delivery directory")
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="production entrypoint (never executed during packaging)")
    parser.add_argument("-o", "--output", type=Path, required=True, help="new client delivery directory")
    parser.add_argument("--name")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--description", default="A packaged production service.")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--include", action="append", default=[], help="additional file relative to project root (repeatable)")
    parser.add_argument("--env", action="append", default=[], help="additional environment variable NAME (repeatable, no values)")
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--runtime-command", type=json.loads, help='JSON string array with one {script} placeholder')
    parser.add_argument("--base-image")
    parser.add_argument("--arg", dest="args", action="append", default=[], help="launch argument; use --arg=--flag")
    parser.add_argument("--smoke-arg", action="append", help="safe smoke argument; use --smoke-arg='' for no arguments")
    parser.add_argument("--port", type=int, help="enable HTTP delivery and expose this container port")
    parser.add_argument("--health-path", default="/health")
    options = vars(parser.parse_args(argv))
    options["smoke_args"] = options.pop("smoke_arg")
    try:
        output = build_bundle(PackageConfig(**options))
    except (PackagingError, OSError) as exc:
        print("Packaging failed:", exc, file=sys.stderr)
        return 1
    print(f"Client delivery ready: {output}")
    print(f"Verify: python3 {shlex.quote(str(output / 'smoke_test.py'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
