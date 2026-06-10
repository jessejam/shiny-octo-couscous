"""Flask UI for running modelaudit scans and streaming their status."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    jsonify,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)


TERMINAL_STATUSES = {
    "finished",
    "failed",
    "timed_out",
    "scanner_unavailable",
    "report_missing",
}

STATUS_META = {
    "queued": {
        "label": "Queued",
        "hint": "The scan request was accepted and is waiting to start.",
    },
    "running": {
        "label": "Running",
        "hint": "The scanner is active. Live status lines should appear below.",
    },
    "finished": {
        "label": "Finished",
        "hint": "The generated scan artifacts are ready to review and download.",
    },
    "failed": {
        "label": "Failed",
        "hint": "The scanner exited with a non-zero status.",
    },
    "timed_out": {
        "label": "Timed Out",
        "hint": "The scan exceeded the configured timeout.",
    },
    "scanner_unavailable": {
        "label": "Scanner Unavailable",
        "hint": "Docker or the scanner runtime is not reachable from this environment.",
    },
    "report_missing": {
        "label": "Missing Output",
        "hint": "The scan finished, but one or more expected output files were not found.",
    },
}

ERROR_META = {
    "failed": {
        "title": "Scan Failed",
        "message": "The modelaudit process exited with a non-zero status.",
        "status_code": 502,
    },
    "timed_out": {
        "title": "Scan Timed Out",
        "message": "The scan ran longer than the configured timeout and was stopped.",
        "status_code": 504,
    },
    "scanner_unavailable": {
        "title": "Scanner Unavailable",
        "message": "Docker or the scanner image is not available in this runtime.",
        "status_code": 503,
    },
    "report_missing": {
        "title": "Missing Output",
        "message": "The scan completed without producing one or more expected output files.",
        "status_code": 502,
    },
}

NETWORK_RESOLUTION_PATTERNS = (
    "temporary failure in name resolution",
    "name or service not known",
    "could not resolve host",
    "failed to resolve",
    "dial tcp",
    "getaddrinfo",
)

REPORT_FILENAME = "report.txt"
SBOM_FILENAME = "sbom.json"
TEXT_MIMETYPE = "text/plain; charset=utf-8"
JSON_MIMETYPE = "application/json"
S3_REQUIRED_ENV_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_KEY_ID", "S3_HOST_BASE")
S3_OPTIONAL_ENV_VARS = (
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
    "AWS_ENDPOINT_URL",
)
SENSITIVE_ENV_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD")


def utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime | None) -> str | None:
    """Format a timestamp for display in the UI."""
    if value is None:
        return None
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def truthy(value: str | None, *, default: bool = False) -> bool:
    """Interpret common environment-style truthy values."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_download_mimetype(path: Path) -> str:
    """Return the preferred download mimetype for an artifact path."""
    return JSON_MIMETYPE if path.suffix == ".json" else TEXT_MIMETYPE


def is_s3_url(value: str) -> bool:
    """Return True when a scan target is an S3 URL."""
    return value.strip().lower().startswith("s3://")


def normalize_endpoint_url(value: str) -> str:
    """Convert a host-style endpoint into a URL accepted by AWS clients."""
    endpoint = value.strip()
    if "://" in endpoint:
        return endpoint
    return f"https://{endpoint}"


def is_sensitive_env_name(name: str) -> bool:
    """Return True when an env var name should not expose its value in UI."""
    normalized = name.upper()
    return any(marker in normalized for marker in SENSITIVE_ENV_MARKERS)


def redact_env_assignment(value: str) -> str:
    """Redact sensitive Docker env assignments before rendering commands."""
    name, separator, _ = value.partition("=")
    if separator and is_sensitive_env_name(name):
        return f"{name}=<redacted>"
    return value


def redact_command_for_display(command: list[str]) -> list[str]:
    """Return a command copy safe to show in templates."""
    redacted: list[str] = []
    previous_arg = ""
    for arg in command:
        if previous_arg in {"-e", "--env"}:
            redacted.append(redact_env_assignment(arg))
        else:
            redacted.append(arg)
        previous_arg = arg
    return redacted


@dataclass(slots=True)
class JobEvent:
    """A single status or log entry emitted during a scan."""

    index: int
    kind: str
    message: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class ScanJob:
    """In-memory representation of one repository scan request."""

    job_id: str
    repo_url: str
    work_dir: Path
    report_path: Path
    sbom_path: Path
    log_path: Path
    created_at: datetime = field(default_factory=utc_now)
    status: str = "queued"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    returncode: int | None = None
    command: list[str] = field(default_factory=list)
    events: list[JobEvent] = field(default_factory=list)
    report_data: Any = None
    condition: threading.Condition = field(
        default_factory=threading.Condition,
        repr=False,
    )

    @property
    def command_preview(self) -> str:
        """Return a shell-friendly version of the configured command."""
        return shlex.join(redact_command_for_display(self.command)) if self.command else ""


class JobStore:
    """Thread-safe storage for scan jobs and their event streams."""

    def __init__(self, scans_root: Path) -> None:
        """Create the job store and remember where scan outputs live."""
        self.scans_root = scans_root
        self._jobs: dict[str, ScanJob] = {}
        self._lock = threading.Lock()

    def create(self, repo_url: str) -> ScanJob:
        """Create a new scan job and its working directory."""
        job_id = uuid.uuid4().hex[:12]
        work_dir = self.scans_root / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        work_dir.chmod(0o777)
        job = ScanJob(
            job_id=job_id,
            repo_url=repo_url.strip(),
            work_dir=work_dir,
            report_path=work_dir / REPORT_FILENAME,
            sbom_path=work_dir / SBOM_FILENAME,
            log_path=work_dir / "scanner.log",
        )
        job.log_path.write_text("", encoding="utf-8")
        with self._lock:
            self._jobs[job_id] = job
        self.add_event(job, "status", "Scan queued.")
        return job

    def get(self, job_id: str) -> ScanJob | None:
        """Look up a scan job by id."""
        with self._lock:
            return self._jobs.get(job_id)

    def add_event(self, job: ScanJob, kind: str, message: str) -> JobEvent:
        """Append a new event to a job and wake any listeners."""
        with job.condition:
            event = JobEvent(
                index=len(job.events),
                kind=kind,
                message=message,
            )
            job.events.append(event)
            job.condition.notify_all()
            return event

    def update_status(
        self,
        job: ScanJob,
        status: str,
        *,
        hint: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update the job status and optionally emit a matching status hint."""
        with job.condition:
            job.status = status
            if status == "running" and job.started_at is None:
                job.started_at = utc_now()
            if status in TERMINAL_STATUSES and job.finished_at is None:
                job.finished_at = utc_now()
            if error_code is not None:
                job.error_code = error_code
            if error_message is not None:
                job.error_message = error_message
            job.condition.notify_all()
        if hint:
            self.add_event(job, "status", hint)

    def snapshot(self, job: ScanJob) -> dict[str, Any]:
        """Build a template-friendly snapshot of the current job state."""
        with job.condition:
            report_text = str(job.report_data) if job.report_data is not None else None
            return {
                "job_id": job.job_id,
                "repo_url": job.repo_url,
                "work_dir": str(job.work_dir),
                "report_path": str(job.report_path),
                "sbom_path": str(job.sbom_path),
                "log_path": str(job.log_path),
                "created_at": format_timestamp(job.created_at),
                "started_at": format_timestamp(job.started_at),
                "finished_at": format_timestamp(job.finished_at),
                "status": job.status,
                "status_label": STATUS_META.get(job.status, {}).get("label", job.status.title()),
                "status_hint": STATUS_META.get(job.status, {}).get("hint", ""),
                "error_code": job.error_code,
                "error_message": job.error_message,
                "returncode": job.returncode,
                "command_preview": job.command_preview,
                "report_text": report_text,
                "report_url": (
                    url_for("download_report", job_id=job.job_id)
                    if job.report_path.exists()
                    else None
                ),
                "sbom_url": (
                    url_for("download_sbom", job_id=job.job_id)
                    if job.sbom_path.exists()
                    else None
                ),
                "log_url": url_for("download_scanner_log", job_id=job.job_id),
                "events": [
                    {
                        "kind": event.kind,
                        "message": event.message,
                        "created_at": format_timestamp(event.created_at),
                    }
                    for event in job.events
                ],
            }


def get_store() -> JobStore:
    """Return the application-wide job store."""
    return current_app.extensions["job_store"]


def get_job_or_404(job_id: str) -> ScanJob:
    """Fetch a job or abort with a 404 page."""
    job = get_store().get(job_id)
    if job is None:
        abort(404)
    return job


def load_report_data(job: ScanJob) -> str:
    """Load and cache the plain-text report for a finished job."""
    with job.condition:
        if job.report_data is not None:
            return job.report_data

    with job.condition:
        job.report_data = read_report_text(job)
        return job.report_data


def read_report_text(job: ScanJob) -> str:
    """Read the raw report file contents without parsing JSON."""
    return job.report_path.read_text(encoding="utf-8", errors="replace")


def append_scanner_log(job: ScanJob, chunk: str) -> None:
    """Append raw scanner output to the per-job log file."""
    with job.log_path.open("a", encoding="utf-8") as handle:
        handle.write(chunk)


def read_scanner_log(job: ScanJob) -> str:
    """Read the raw scanner log for a job."""
    return job.log_path.read_text(encoding="utf-8", errors="replace")


def build_s3_container_env(config: dict[str, Any]) -> tuple[list[str], dict[str, str], list[str]]:
    """Resolve S3 credentials and compatibility env vars for Docker."""
    passthrough_vars: list[str] = []
    explicit_vars: dict[str, str] = {}
    missing_required: list[str] = []

    access_key = os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_KEY_ID")
    standard_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    host_base = os.getenv("S3_HOST_BASE")

    if access_key:
        passthrough_vars.append("AWS_ACCESS_KEY_ID")
    else:
        missing_required.append("AWS_ACCESS_KEY_ID")

    if secret_key:
        passthrough_vars.append("AWS_SECRET_KEY_ID")
        if standard_secret_key:
            passthrough_vars.append("AWS_SECRET_ACCESS_KEY")
        else:
            explicit_vars["AWS_SECRET_ACCESS_KEY"] = secret_key
    else:
        missing_required.append("AWS_SECRET_KEY_ID")

    if host_base:
        passthrough_vars.append("S3_HOST_BASE")
        if not os.getenv("AWS_ENDPOINT_URL"):
            explicit_vars["AWS_ENDPOINT_URL"] = normalize_endpoint_url(host_base)
    else:
        missing_required.append("S3_HOST_BASE")

    for name in (*S3_OPTIONAL_ENV_VARS, *config["OPTIONAL_CONTAINER_ENV_VARS"]):
        if os.getenv(name) and name not in passthrough_vars and name not in explicit_vars:
            passthrough_vars.append(name)

    for name, default_value in config["CONTAINER_ENV_DEFAULTS"].items():
        explicit_vars[name] = os.getenv(name, default_value)

    return passthrough_vars, explicit_vars, missing_required


def build_container_env(
    config: dict[str, Any],
    repo_url: str,
) -> tuple[list[str], dict[str, str], list[str]]:
    """Resolve which environment variables should be forwarded into Docker."""
    if is_s3_url(repo_url):
        return build_s3_container_env(config)

    passthrough_vars: list[str] = []
    explicit_vars: dict[str, str] = {}
    missing_required: list[str] = []

    for name in config["REQUIRED_CONTAINER_ENV_VARS"]:
        if os.getenv(name):
            passthrough_vars.append(name)
        else:
            missing_required.append(name)

    for name in config["OPTIONAL_CONTAINER_ENV_VARS"]:
        if os.getenv(name):
            passthrough_vars.append(name)

    for name, default_value in config["CONTAINER_ENV_DEFAULTS"].items():
        explicit_vars[name] = os.getenv(name, default_value)

    return passthrough_vars, explicit_vars, missing_required


def build_docker_command(job: ScanJob, config: dict[str, Any]) -> list[str]:
    """Construct the Docker command used to run modelaudit."""
    container_workdir = config["CONTAINER_WORKDIR"]
    command = [config["DOCKER_BINARY"], "run", "--rm"]
    passthrough_vars, explicit_vars, _ = build_container_env(config, job.repo_url)
    scanner_args = [
        "--verbose",
        "--format",
        "text",
        "--sbom",
        job.sbom_path.name,
        "--timeout",
        str(config["SCAN_TIMEOUT_SECONDS"]),
        "--output",
        job.report_path.name,
    ]

    if config["DOCKER_NETWORK_MODE"]:
        command.extend(["--network", config["DOCKER_NETWORK_MODE"]])

    if config["USE_EMPTY_ENTRYPOINT"]:
        command.extend(["--entrypoint", ""])

    for env_name in passthrough_vars:
        command.extend(["-e", env_name])

    for env_name, env_value in explicit_vars.items():
        command.extend(["-e", f"{env_name}={env_value}"])

    command.extend(
        [
            "-v",
            f"{job.work_dir}:{container_workdir}",
            "-w",
            container_workdir,
        ]
    )

    command.extend(config["EXTRA_DOCKER_ARGS"])
    command.append(config["SCANNER_IMAGE"])
    if config["SCANNER_COMMAND"]:
        command.append(config["SCANNER_COMMAND"])
    command.extend(scanner_args)
    command.append(job.repo_url)
    return command


def build_failed_scan_message(job: ScanJob, config: dict[str, Any], returncode: int) -> str:
    """Create a more actionable failure message from recent scanner output."""
    message = f"The scanner exited with status code {returncode}."

    with job.condition:
        recent_logs = [
            event.message
            for event in job.events
            if event.kind in {"log", "error"}
        ][-25:]

    combined_logs = "\n".join(recent_logs).lower()
    if not any(pattern in combined_logs for pattern in NETWORK_RESOLUTION_PATTERNS):
        return message

    docker_network_mode = config["DOCKER_NETWORK_MODE"].strip().lower()
    if docker_network_mode == "none":
        return (
            message
            + " The scanner also reported a DNS lookup failure while the container was started "
            + 'with Docker network mode "none", which disables outbound network access. A curl '
            + "request from the host can still succeed because it is not running inside that "
            + "isolated container. Set DOCKER_NETWORK_MODE=bridge, or leave it empty to use "
            + "Docker's default network, when the scan must reach remote repository hosts."
        )

    return (
        message
        + " The scanner also reported a DNS lookup failure inside the container. Verify that "
        + "the container can resolve the repository host, and if needed pass DNS or host "
        + "mapping flags through EXTRA_DOCKER_ARGS."
    )


def fail_job(
    store: JobStore,
    job: ScanJob,
    *,
    status: str,
    error_code: str,
    message: str,
) -> None:
    """Move a job into a terminal error state and record the reason."""
    store.update_status(
        job,
        status,
        error_code=error_code,
        error_message=message,
    )
    store.add_event(job, "error", message)
    store.add_event(job, "done", "Scan stopped.")


def stream_process_output(process: subprocess.Popen[str], store: JobStore, job: ScanJob) -> None:
    """Read scanner output line by line and publish it as job events."""
    if process.stdout is None:
        return

    for raw_line in iter(process.stdout.readline, ""):
        append_scanner_log(job, raw_line)
        sys.stdout.write(f"[scan {job.job_id}] {raw_line}")
        sys.stdout.flush()
        line = raw_line.strip()
        if line:
            store.add_event(job, "log", line)

    process.stdout.close()


def run_mock_scan(store: JobStore, job: ScanJob) -> None:
    """Simulate a scan so the UI can be tested without Docker."""
    with job.condition:
        job.command = [
            "mock-scan",
            "modelaudit",
            "scan",
            "--verbose",
            "--format",
            "text",
            "--sbom",
            job.sbom_path.name,
            "--timeout",
            str(current_app.config["SCAN_TIMEOUT_SECONDS"]),
            "--output",
            job.report_path.name,
            job.repo_url,
        ]

    store.update_status(job, "running", hint="Mock scan started.")

    mock_steps = [
        "Preparing isolated scan workspace.",
        "Resolving repository metadata.",
        "Inspecting model files.",
        "Collecting policy findings.",
        f"Writing {job.sbom_path.name}.",
        f"Writing {job.report_path.name}.",
    ]

    for step in mock_steps:
        store.add_event(job, "log", step)
        time.sleep(0.9)

    report = """\
SCAN SUMMARY
Duration: 0.532s
Files: 1
Format: text

SECURITY FINDINGS
- Mock finding generated for UI development.
"""
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"name": "mock-model"}},
    }

    job.report_path.write_text(report, encoding="utf-8")
    job.sbom_path.write_text(json.dumps(sbom, indent=2), encoding="utf-8")
    with job.condition:
        job.report_data = report
        job.returncode = 0

    store.update_status(job, "finished", hint="Mock scan finished. Artifacts ready.")
    store.add_event(job, "done", "Scan complete.")


def run_docker_scan(store: JobStore, job: ScanJob, config: dict[str, Any]) -> None:
    """Execute a real scanner container and capture its report and logs."""
    docker_binary = config["DOCKER_BINARY"]
    if shutil.which(docker_binary) is None:
        fail_job(
            store,
            job,
            status="scanner_unavailable",
            error_code="scanner_unavailable",
            message=f"The `{docker_binary}` executable is not available in this runtime.",
        )
        return

    _, _, missing_required = build_container_env(config, job.repo_url)
    if missing_required:
        fail_job(
            store,
            job,
            status="failed",
            error_code="missing_environment",
            message=(
                "Missing required environment variables for the scanner container: "
                + ", ".join(missing_required)
            ),
        )
        return

    command = build_docker_command(job, config)

    with job.condition:
        job.command = command

    store.update_status(job, "running", hint="Scan started.")
    store.add_event(job, "log", f"Launching container with image `{config['SCANNER_IMAGE']}`.")

    try:
        process = subprocess.Popen(
            command,
            cwd=job.work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        fail_job(
            store,
            job,
            status="scanner_unavailable",
            error_code="scanner_unavailable",
            message=f"Failed to start the scanner process: {exc}.",
        )
        return

    reader_thread = threading.Thread(
        target=stream_process_output,
        args=(process, store, job),
        daemon=True,
    )
    reader_thread.start()

    timeout_seconds = config["SCAN_TIMEOUT_SECONDS"]
    deadline = time.monotonic() + timeout_seconds
    timed_out = False

    while True:
        returncode = process.poll()
        if returncode is not None:
            break

        if time.monotonic() >= deadline:
            timed_out = True
            process.kill()
            break

        time.sleep(0.25)

    returncode = process.wait()
    reader_thread.join(timeout=5)

    with job.condition:
        job.returncode = returncode

    if timed_out:
        fail_job(
            store,
            job,
            status="timed_out",
            error_code="timed_out",
            message=f"The scan exceeded the {timeout_seconds}-second timeout.",
        )
        return

    if returncode != 0:
        fail_job(
            store,
            job,
            status="failed",
            error_code="failed",
            message=build_failed_scan_message(job, config, returncode),
        )
        return

    if not job.report_path.exists():
        fail_job(
            store,
            job,
            status="report_missing",
            error_code="report_missing",
            message=f"The scan completed, but no report file was created at {job.report_path.name}.",
        )
        return

    if not job.sbom_path.exists():
        fail_job(
            store,
            job,
            status="report_missing",
            error_code="report_missing",
            message=f"The scan completed, but no SBOM file was created at {job.sbom_path.name}.",
        )
        return

    report_text = read_report_text(job)

    with job.condition:
        job.report_data = report_text

    store.update_status(
        job,
        "finished",
        hint="Scan finished. Report and SBOM are ready.",
    )
    store.add_event(job, "done", "Scan complete.")


def run_scan_job(app: Flask, job_id: str) -> None:
    """Run a job in the background using the active scanner mode."""
    with app.app_context():
        store = get_store()
        job = store.get(job_id)
        if job is None:
            return

        config_snapshot = {
            "SCANNER_MODE": current_app.config["SCANNER_MODE"],
            "DOCKER_BINARY": current_app.config["DOCKER_BINARY"],
            "SCANNER_IMAGE": current_app.config["SCANNER_IMAGE"],
            "SCANNER_COMMAND": current_app.config["SCANNER_COMMAND"],
            "REQUIRED_CONTAINER_ENV_VARS": current_app.config["REQUIRED_CONTAINER_ENV_VARS"],
            "OPTIONAL_CONTAINER_ENV_VARS": current_app.config["OPTIONAL_CONTAINER_ENV_VARS"],
            "CONTAINER_ENV_DEFAULTS": current_app.config["CONTAINER_ENV_DEFAULTS"],
            "DOCKER_NETWORK_MODE": current_app.config["DOCKER_NETWORK_MODE"],
            "USE_EMPTY_ENTRYPOINT": current_app.config["USE_EMPTY_ENTRYPOINT"],
            "EXTRA_DOCKER_ARGS": current_app.config["EXTRA_DOCKER_ARGS"],
            "CONTAINER_WORKDIR": current_app.config["CONTAINER_WORKDIR"],
            "SCAN_TIMEOUT_SECONDS": current_app.config["SCAN_TIMEOUT_SECONDS"],
        }

        try:
            if config_snapshot["SCANNER_MODE"] == "mock":
                run_mock_scan(store, job)
                return

            run_docker_scan(store, job, config_snapshot)
        except Exception as exc:
            with job.condition:
                if job.status in TERMINAL_STATUSES:
                    return
            fail_job(
                store,
                job,
                status="failed",
                error_code="failed",
                message=f"Unexpected scanner error: {exc}.",
            )


def format_sse(event_name: str, payload: dict[str, Any]) -> str:
    """Serialize one server-sent event payload."""
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_name}\ndata: {data}\n\n"


def build_job_update_payload(job: ScanJob, events: list[JobEvent]) -> dict[str, Any]:
    """Build an update payload shared by SSE and polling fallbacks."""
    with job.condition:
        status = job.status
        return {
            "events": [
                {
                    "kind": event.kind,
                    "message": event.message,
                    "created_at": format_timestamp(event.created_at),
                }
                for event in events
            ],
            "event_count": len(job.events),
            "status": status,
            "status_label": STATUS_META.get(status, {}).get("label", status.title()),
            "status_hint": STATUS_META.get(status, {}).get("hint", ""),
            "result_url": url_for("job_result", job_id=job.job_id),
            "report_url": (
                url_for("download_report", job_id=job.job_id)
                if job.report_path.exists()
                else None
            ),
            "sbom_url": (
                url_for("download_sbom", job_id=job.job_id)
                if job.sbom_path.exists()
                else None
            ),
        }


def job_event_stream(job: ScanJob, start_index: int = 0) -> Any:
    """Yield live job events for the browser SSE connection."""
    last_index = max(0, start_index)
    yield "retry: 1500\n\n"

    while True:
        with job.condition:
            if last_index >= len(job.events) and job.status not in TERMINAL_STATUSES:
                job.condition.wait(timeout=10)

            current_events = job.events[last_index:]
            last_index = len(job.events)
            status = job.status

        if current_events:
            for event in current_events:
                payload = build_job_update_payload(job, [event])
                yield format_sse(
                    event.kind,
                    payload["events"][0]
                    | {
                        "status": payload["status"],
                        "status_label": payload["status_label"],
                        "status_hint": payload["status_hint"],
                        "result_url": payload["result_url"],
                        "report_url": payload["report_url"],
                        "sbom_url": payload["sbom_url"],
                    },
                )
        else:
            yield ": keep-alive\n\n"

        with job.condition:
            if job.status in TERMINAL_STATUSES and last_index >= len(job.events):
                break


def render_job_error(job: ScanJob) -> tuple[str, int]:
    """Render the appropriate error page for a failed job."""
    snapshot = get_store().snapshot(job)
    meta = ERROR_META.get(
        snapshot["status"],
        {
            "title": "Unexpected Error",
            "message": "The scan did not complete successfully.",
            "status_code": 500,
        },
    )
    message = snapshot["error_message"] or meta["message"]
    return (
        render_template(
            "error.html",
            title=meta["title"],
            headline=meta["title"],
            message=message,
            job=snapshot,
            action_label="Start another scan",
            action_url=url_for("index"),
            secondary_action_label="Back to live scan",
            secondary_action_url=url_for("job_detail", job_id=job.job_id),
        ),
        meta["status_code"],
    )


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)

    scans_root = Path(app.instance_path) / "scans"
    scans_root.mkdir(parents=True, exist_ok=True)

    app.config.update(
        SCANNER_MODE=os.getenv("SCANNER_MODE", "docker").strip().lower(),
        DOCKER_BINARY=os.getenv("DOCKER_BINARY", "docker"),
        SCANNER_IMAGE=os.getenv("SCANNER_IMAGE", "ghcr.io/promptfoo/modelaudit:latest"),
        SCANNER_COMMAND=os.getenv("SCANNER_COMMAND", "scan").strip(),
        REQUIRED_CONTAINER_ENV_VARS=shlex.split(
            os.getenv("REQUIRED_CONTAINER_ENV_VARS", "JFROG_URL JFROG_API_TOKEN")
        ),
        OPTIONAL_CONTAINER_ENV_VARS=shlex.split(
            os.getenv("OPTIONAL_CONTAINER_ENV_VARS", "")
        ),
        CONTAINER_ENV_DEFAULTS={
            "PROMPTFOO_DISABLE_TELEMETRY": os.getenv("PROMPTFOO_DISABLE_TELEMETRY", "1"),
            "NO_ANALYTICS": os.getenv("NO_ANALYTICS", "1"),
        },
        DOCKER_NETWORK_MODE=os.getenv("DOCKER_NETWORK_MODE", ""),
        USE_EMPTY_ENTRYPOINT=truthy(os.getenv("USE_EMPTY_ENTRYPOINT"), default=False),
        EXTRA_DOCKER_ARGS=shlex.split(os.getenv("EXTRA_DOCKER_ARGS", "")),
        CONTAINER_WORKDIR=os.getenv("CONTAINER_WORKDIR", "/work"),
        SCAN_TIMEOUT_SECONDS=int(os.getenv("SCAN_TIMEOUT_SECONDS", "5400")),
    )

    app.extensions["job_store"] = JobStore(scans_root=scans_root)

    @app.get("/")
    def index() -> str:
        """Render the main page with scanner runtime details."""
        docker_network_mode = current_app.config["DOCKER_NETWORK_MODE"]
        runtime = {
            "scanner_mode": current_app.config["SCANNER_MODE"],
            "scanner_image": current_app.config["SCANNER_IMAGE"],
            "required_env_vars": current_app.config["REQUIRED_CONTAINER_ENV_VARS"],
            "s3_required_env_vars": S3_REQUIRED_ENV_VARS,
            "container_env_defaults": current_app.config["CONTAINER_ENV_DEFAULTS"],
            "docker_network_mode": docker_network_mode or "default",
            "docker_network_summary": (
                "Outbound DNS and network access are disabled for scan containers. "
                "Remote repository URLs can fail here even when a host-side curl succeeds."
                if docker_network_mode == "none"
                else (
                    "Scans use Docker's default container network."
                    if not docker_network_mode
                    else "Scans use the configured Docker network mode for outbound access."
                )
            ),
            "extra_docker_args": shlex.join(current_app.config["EXTRA_DOCKER_ARGS"])
            if current_app.config["EXTRA_DOCKER_ARGS"]
            else "none",
            "timeout_seconds": current_app.config["SCAN_TIMEOUT_SECONDS"],
        }
        return render_template("index.html", runtime=runtime)

    @app.post("/scan")
    def start_scan() -> Response | str:
        """Create a scan job and redirect to its live progress page."""
        repo_url = request.form.get("repo_url", "").strip()
        if not repo_url:
            return (
                render_template(
                    "error.html",
                    title="Repository URL Required",
                    headline="Repository URL Required",
                    message="Enter the corporate repository URL before starting a scan.",
                    action_label="Back to scanner",
                    action_url=url_for("index"),
                ),
                400,
            )

        store = get_store()
        job = store.create(repo_url)

        thread = threading.Thread(
            target=run_scan_job,
            args=(current_app._get_current_object(), job.job_id),
            daemon=True,
        )
        thread.start()

        return redirect(url_for("job_detail", job_id=job.job_id))

    @app.get("/jobs/<job_id>")
    def job_detail(job_id: str) -> str:
        """Show the live status page for a scan job."""
        snapshot = get_store().snapshot(get_job_or_404(job_id))
        return render_template("job.html", job=snapshot)

    @app.get("/jobs/<job_id>/events")
    def job_events(job_id: str) -> Response:
        """Stream incremental job events to the browser via SSE."""
        job = get_job_or_404(job_id)
        start_index = request.args.get("from", default=0, type=int)
        return Response(
            job_event_stream(job, start_index=start_index),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/jobs/<job_id>/poll")
    def job_poll(job_id: str) -> Response:
        """Return incremental job updates as JSON for polling fallback."""
        job = get_job_or_404(job_id)
        start_index = request.args.get("from", default=0, type=int)
        with job.condition:
            events = job.events[start_index:]
        return jsonify(build_job_update_payload(job, events))

    @app.get("/jobs/<job_id>/result")
    def job_result(job_id: str) -> Response | str:
        """Show the final report page or a terminal error page."""
        job = get_job_or_404(job_id)
        with job.condition:
            status = job.status

        if status == "finished":
            report_text = load_report_data(job)
            snapshot = get_store().snapshot(job)
            return render_template(
                "report.html",
                job=snapshot,
                report_text=report_text,
            )

        if status in TERMINAL_STATUSES:
            return render_job_error(job)

        return redirect(url_for("job_detail", job_id=job.job_id))

    @app.get("/jobs/<job_id>/download")
    def download_report(job_id: str) -> Response:
        """Download the finished report file for a job."""
        job = get_job_or_404(job_id)
        with job.condition:
            if job.status != "finished" or not job.report_path.exists():
                abort(404)

        return send_file(
            job.report_path,
            as_attachment=True,
            download_name=f"{job.job_id}-{job.report_path.name}",
            mimetype=get_download_mimetype(job.report_path),
        )

    @app.get("/jobs/<job_id>/download-sbom")
    def download_sbom(job_id: str) -> Response:
        """Download the JSON SBOM file for a job."""
        job = get_job_or_404(job_id)
        if not job.sbom_path.exists():
            abort(404)

        return send_file(
            job.sbom_path,
            as_attachment=True,
            download_name=f"{job.job_id}-{job.sbom_path.name}",
            mimetype=get_download_mimetype(job.sbom_path),
        )

    @app.get("/jobs/<job_id>/download-raw")
    def download_raw_report(job_id: str) -> Response:
        """Download the plain-text report file regardless of parse state."""
        job = get_job_or_404(job_id)
        if not job.report_path.exists():
            abort(404)

        return send_file(
            job.report_path,
            as_attachment=True,
            download_name=f"{job.job_id}-{job.report_path.name}",
            mimetype=get_download_mimetype(job.report_path),
        )

    @app.get("/jobs/<job_id>/download-log")
    def download_scanner_log(job_id: str) -> Response:
        """Download the raw scanner stdout/stderr log for a job."""
        job = get_job_or_404(job_id)
        if not job.log_path.exists():
            abort(404)

        return send_file(
            job.log_path,
            as_attachment=True,
            download_name=f"{job.job_id}-scanner.log",
            mimetype="text/plain; charset=utf-8",
        )

    @app.errorhandler(404)
    def handle_not_found(_: Exception) -> tuple[str, int]:
        """Render the generic not-found page."""
        return (
            render_template(
                "error.html",
                title="Not Found",
                headline="Not Found",
                message="The page or scan job you requested does not exist.",
                action_label="Back to scanner",
                action_url=url_for("index"),
            ),
            404,
        )

    @app.errorhandler(500)
    def handle_server_error(_: Exception) -> tuple[str, int]:
        """Render the generic internal-error page."""
        return (
            render_template(
                "error.html",
                title="Unexpected Error",
                headline="Unexpected Error",
                message="Something went wrong inside the Flask app.",
                action_label="Back to scanner",
                action_url=url_for("index"),
            ),
            500,
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
