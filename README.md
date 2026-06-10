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
export SCANNER_IMAGE=ghcr.io/promptfoo/modelaudit:latest
python3 main.py
```

For an S3-backed scan target such as `s3://bucket/path/model.bin`, export the S3 credentials
instead of the JFrog credentials:

```bash
export AWS_ACCESS_KEY_ID="your-access-key"
export AWS_SECRET_KEY_ID="your-secret-key"
export S3_HOST_BASE="https://s3.example.internal"
export PROMPTFOO_DISABLE_TELEMETRY=1
export NO_ANALYTICS=1
export SCANNER_MODE=docker
export DOCKER_NETWORK_MODE=bridge
export SCANNER_IMAGE=ghcr.io/promptfoo/modelaudit:latest
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
- `SCANNER_IMAGE`: Docker image name, default `ghcr.io/promptfoo/modelaudit:latest`
- `SCANNER_COMMAND`: command inside the container, default `scan`; set empty only if your image entrypoint already includes the scan command
- `REQUIRED_CONTAINER_ENV_VARS`: env vars that must exist before Docker scans start, default `JFROG_URL JFROG_API_TOKEN`
- S3 scan env vars: when the submitted target starts with `s3://`, the app requires `AWS_ACCESS_KEY_ID`, `AWS_SECRET_KEY_ID`, and `S3_HOST_BASE` instead of the default JFrog env vars
- `OPTIONAL_CONTAINER_ENV_VARS`: extra env vars to forward into the container when present
- `PROMPTFOO_DISABLE_TELEMETRY`: forwarded into the container, default `1`
- `NO_ANALYTICS`: forwarded into the container, default `1`
- `DOCKER_NETWORK_MODE`: Docker network mode, default empty; set `none` only when scans should not
  have outbound DNS or network access
- `USE_EMPTY_ENTRYPOINT`: `false` by default; set it only if you need to override a broken image entrypoint
- `EXTRA_DOCKER_ARGS`: additional raw Docker flags such as `--add-host` or `--security-opt seccomp=...`
- `CONTAINER_WORKDIR`: mounted working directory inside the container, default `/work`
- `SCAN_TIMEOUT_SECONDS`: max duration per scan, default `5400`; the same value is also passed to modelaudit as `--timeout`

The app passes these scanner arguments itself: `--format text`, `--sbom sbom.json`,
`--output report.txt`, and `--timeout`.

Each scan always produces these files:

- `report.txt`: the short plain-text report shown to the user
- `sbom.json`: the generated SBOM output
- `scanner.log`: the raw stdout/stderr stream captured by the app

## S3 Scans

When the submitted target starts with `s3://`, the app uses S3-specific credentials instead of
the default JFrog required env vars. Export these before starting Flask:

```bash
export AWS_ACCESS_KEY_ID="your-access-key"
export AWS_SECRET_KEY_ID="your-secret-key"
export S3_HOST_BASE="https://s3.example.internal"
python3 main.py
```

For compatibility with standard ModelAudit/AWS clients, the app forwards `AWS_SECRET_KEY_ID` as
`AWS_SECRET_ACCESS_KEY` when the standard variable is not already set, and maps `S3_HOST_BASE` to
`AWS_ENDPOINT_URL` when `AWS_ENDPOINT_URL` is not already set.

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
