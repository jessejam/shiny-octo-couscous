# ModelAudit Runner

A small Flask UI that starts a repository scan, streams incremental modelaudit output to the browser, and renders the resulting `report.json`.

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
- `DOCKER_BINARY`: Docker executable name, default `docker`
- `SCANNER_IMAGE`: Docker image name, default `modelaudit`
- `SCANNER_COMMAND`: command inside the container, default `modelaudit`
- `SCANNER_FIXED_ARGS`: extra scanner arguments before the repository URL; the app always adds mandatory `--stream`
- `REQUIRED_CONTAINER_ENV_VARS`: env vars that must exist before Docker scans start, default `JFROG_URL JFROG_API_TOKEN`
- `OPTIONAL_CONTAINER_ENV_VARS`: extra env vars to forward into the container when present
- `PROMPTFOO_DISABLE_TELEMETRY`: forwarded into the container, default `1`
- `NO_ANALYTICS`: forwarded into the container, default `1`
- `DOCKER_NETWORK_MODE`: Docker network mode, default `none`; this blocks outbound DNS and network
  access from the container, so set `bridge` or leave it empty when scans need to reach remote
  hosts
- `USE_EMPTY_ENTRYPOINT`: `true` by default to inject `--entrypoint ""`
- `EXTRA_DOCKER_ARGS`: additional raw Docker flags such as `--add-host` or `--security-opt seccomp=...`
- `CONTAINER_WORKDIR`: mounted working directory inside the container, default `/work`
- `SCAN_OUTPUT_FILENAME`: output filename, default `report.json`
- `SCAN_TIMEOUT_SECONDS`: max duration per scan, default `1800`

## External Validation Ideas

Try these Docker variants in the environment where the scanner image exists:

1. Baseline:
   `DOCKER_NETWORK_MODE=none`
2. Faster host failures:
   `EXTRA_DOCKER_ARGS="--add-host host1=127.0.0.1 --add-host host2=127.0.0.1"`
3. Syscall-level hard stop:
   `EXTRA_DOCKER_ARGS="--security-opt seccomp=/absolute/path/to/profile.json"`

The app already streams whatever `modelaudit` prints line by line, so the main thing to validate is how quickly those network attempts fail under each Docker profile.
