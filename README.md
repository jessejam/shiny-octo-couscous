# ModelAudit Runner

A small Flask UI that starts a repository scan, streams incremental modelaudit output to the browser, and renders the resulting report file.

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

For a real Docker-backed scan, export the required credentials and the telemetry flags before starting Flask:

```bash
export JFROG_URL="https://your-jfrog-host"
export JFROG_API_TOKEN="your-token"
export PROMPTFOO_DISABLE_TELEMETRY=1
export NO_ANALYTICS=1
export SCANNER_MODE=docker
export DOCKER_NETWORK_MODE=bridge
export SCANNER_IMAGE=modelaudit
python3 main.py
```

Use `DOCKER_NETWORK_MODE=bridge` (or leave it empty to use Docker's default network) when the
scanner must reach a remote repository host. If the host machine can `curl` successfully but the
scan reports `Temporary failure in name resolution`, the container is likely running with
`DOCKER_NETWORK_MODE=none`.

For a UI-only local check without Docker:

```bash
export SCANNER_MODE=mock
python3 main.py
```

## Configuration

- `SCANNER_MODE`: `docker` or `mock`
- `DEFAULT_OUTPUT_FORMAT`: default UI selection for scan output, one of `text`, `json`, or `sarif`
- `DOCKER_BINARY`: Docker executable name, default `docker`
- `SCANNER_IMAGE`: Docker image name, default `modelaudit`
- `SCANNER_COMMAND`: optional command inside the container; leave empty to use the image entrypoint
- `SCANNER_FIXED_ARGS`: extra scanner arguments before the repository URL; the app always adds mandatory `--stream` and `--timeout`, and the UI selection controls `--format`, `--sbom`, and `--output`
- `REQUIRED_CONTAINER_ENV_VARS`: env vars that must exist before Docker scans start, default `JFROG_URL JFROG_API_TOKEN`
- `OPTIONAL_CONTAINER_ENV_VARS`: extra env vars to forward into the container when present
- `PROMPTFOO_DISABLE_TELEMETRY`: forwarded into the container, default `1`
- `NO_ANALYTICS`: forwarded into the container, default `1`
- `DOCKER_NETWORK_MODE`: Docker network mode, default `none`; this blocks outbound DNS and network
  access from the container, so set `bridge` or leave it empty when scans need to reach remote
  hosts
- `USE_EMPTY_ENTRYPOINT`: `false` by default; set it only if you need to override a broken image entrypoint
- `EXTRA_DOCKER_ARGS`: additional raw Docker flags such as `--add-host` or `--security-opt seccomp=...`
- `CONTAINER_WORKDIR`: mounted working directory inside the container, default `/work`
- `SCAN_TIMEOUT_SECONDS`: max duration per scan, default `5400`; the same value is also passed to modelaudit as `--timeout`

In the web form, the user can choose `text`, `json`, or `sarif`. That choice changes:

- the scanner `--format` flag
- the report filename extension, for example `report.txt`, `report.json`, or `report.sarif`
- the SBOM filename extension, for example `sbom.txt`, `sbom.json`, or `sbom.sarif`

## Passing Extra Env Vars Into The Container

If the scanner needs additional environment variables, export them in your shell and list their
names in `OPTIONAL_CONTAINER_ENV_VARS`.

```bash
export MY_FLAG=1
export API_BASE_URL="https://example.internal"
export OPTIONAL_CONTAINER_ENV_VARS="MY_FLAG API_BASE_URL"
python3 main.py
```

The app forwards those names to Docker as `-e MY_FLAG -e API_BASE_URL`, so the container receives
the values from your current shell environment.

To add extra Docker flags to `docker run`, export `EXTRA_DOCKER_ARGS` alongside your env vars.

```bash
export MY_FLAG=1
export OPTIONAL_CONTAINER_ENV_VARS="MY_FLAG"
export EXTRA_DOCKER_ARGS="--add-host repo.internal=127.0.0.1 --security-opt seccomp=/absolute/path/profile.json"
python3 main.py
```

## External Validation Ideas

Try these Docker variants in the environment where the scanner image exists:

1. Baseline:
   `DOCKER_NETWORK_MODE=none`
2. Faster host failures:
   `EXTRA_DOCKER_ARGS="--add-host host1=127.0.0.1 --add-host host2=127.0.0.1"`
3. Syscall-level hard stop:
   `EXTRA_DOCKER_ARGS="--security-opt seccomp=/absolute/path/to/profile.json"`

The app already streams whatever `modelaudit` prints line by line, so the main thing to validate is how quickly those network attempts fail under each Docker profile.
