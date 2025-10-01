import json
import atexit
import logging
import os
import shlex
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"
UPLOAD_FOLDER = DATA_DIR / "uploads"
OUTPUT_FOLDER = DATA_DIR / "outputs"
METADATA_FILENAME = "metadata.json"

UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {"mp4"}
MAX_PARTS = 4
MIN_PARTS = 2

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["OUTPUT_FOLDER"] = str(OUTPUT_FOLDER)
app.logger.setLevel(logging.INFO)

MAX_WORKER_THREADS = max(1, int(os.environ.get("WORKER_THREADS", "2")))
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKER_THREADS, thread_name_prefix="video-worker")


def _shutdown_executor() -> None:
    EXECUTOR.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown_executor)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def run_command(command: list[str], *, description: str | None = None) -> subprocess.CompletedProcess:
    command_text = shlex.join(command)
    label = description or "command"
    app.logger.info("Running %s: %s", label, command_text)
    start_time = time.perf_counter()
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    elapsed = time.perf_counter() - start_time
    app.logger.info("Finished %s in %.2f seconds", label, elapsed)

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if stdout:
        app.logger.debug("%s stdout: %s", label, stdout)
    if stderr:
        app.logger.warning("%s stderr: %s", label, stderr)

    return result


def get_duration_seconds(file_path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(file_path),
    ]
    result = run_command(command, description="ffprobe duration lookup")

    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise ValueError(f"Unable to determine duration for {file_path}") from exc

    if duration <= 0:
        raise ValueError(f"Invalid video duration ({duration}) for {file_path}")

    app.logger.info("Duration for %s: %.2f seconds", file_path, duration)
    return duration


def split_video(file_path: Path, output_dir: Path, parts: int) -> list[Path]:
    duration = get_duration_seconds(file_path)
    part_duration = duration / parts

    output_files: list[Path] = []
    for index in range(parts):
        start_time = part_duration * index
        output_file = output_dir / f"{file_path.stem}_part{index + 1}{file_path.suffix}"

        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(file_path),
            "-ss",
            f"{start_time:.2f}",
            "-c",
            "copy",
        ]

        if index < parts - 1:
            command.extend(["-t", f"{part_duration:.2f}"])

        command.append(str(output_file))
        run_command(command, description=f"ffmpeg split part {index + 1}/{parts}")
        output_files.append(output_file)

    app.logger.info("Completed splitting %s into %d parts", file_path, parts)
    return output_files


def save_metadata(output_dir: Path, data: dict) -> None:
    metadata_path = output_dir / METADATA_FILENAME
    temp_path = metadata_path.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as metadata_file:
        json.dump(data, metadata_file, indent=2)
    temp_path.replace(metadata_path)
    app.logger.info("Saved metadata to %s", metadata_path)


def load_metadata(output_dir: Path) -> dict | None:
    metadata_path = output_dir / METADATA_FILENAME
    if not metadata_path.exists():
        return None
    try:
        with metadata_path.open(encoding="utf-8") as metadata_file:
            return json.load(metadata_file)
    except json.JSONDecodeError:
        app.logger.exception("Failed to parse metadata at %s", metadata_path)
        return None


def build_job_payload(job_id: str, output_dir: Path) -> dict:
    metadata = load_metadata(output_dir) or {}
    status = metadata.get("status", "unknown")

    files: list[str] = []
    if status == "completed":
        files = metadata.get("outputs", [])
        if not files:
            files = [
                file.name
                for file in output_dir.iterdir()
                if file.is_file() and file.name != METADATA_FILENAME
            ]
            files.sort()

    payload = {
        "job_id": job_id,
        "status": status,
        "metadata": metadata,
        "files": files,
    }

    if status == "completed" and metadata.get("outputs") != files:
        metadata = dict(metadata)
        metadata["outputs"] = files
        payload["metadata"] = metadata

    return payload


def process_job(
    job_id: str,
    saved_file: Path,
    output_dir: Path,
    parts: int,
    base_metadata: dict,
) -> None:
    metadata = dict(base_metadata)
    app.logger.info("Starting background processing for job %s", job_id)

    try:
        output_files = split_video(saved_file, output_dir, parts)
    except (subprocess.CalledProcessError, ValueError) as error:
        metadata.update(
            {
                "status": "error",
                "error_message": str(error),
                "completed_at": iso_now(),
                "outputs": [],
            }
        )
        save_metadata(output_dir, metadata)
        if isinstance(error, subprocess.CalledProcessError):
            app.logger.error(
                "Job %s failed while splitting video: %s", job_id, error.stderr
            )
        else:
            app.logger.error(
                "Job %s failed while determining duration: %s", job_id, error
            )
    except Exception:  # noqa: BLE001
        metadata.update(
            {
                "status": "error",
                "error_message": "Unexpected error while processing the video.",
                "completed_at": iso_now(),
                "outputs": [],
            }
        )
        save_metadata(output_dir, metadata)
        app.logger.exception("Job %s encountered an unexpected error", job_id)
    else:
        metadata.update(
            {
                "status": "completed",
                "completed_at": iso_now(),
                "outputs": [file.name for file in output_files],
            }
        )
        save_metadata(output_dir, metadata)
        app.logger.info(
            "Job %s completed in background with %d outputs", job_id, len(output_files)
        )
    finally:
        try:
            saved_file.unlink(missing_ok=True)
        except OSError:
            app.logger.warning(
                "Unable to remove uploaded file %s after processing job %s",
                saved_file,
                job_id,
                exc_info=True,
            )
        try:
            saved_file.parent.rmdir()
        except OSError:
            app.logger.debug(
                "Upload directory %s not removed (may not be empty)", saved_file.parent
            )


@app.route("/", methods=["GET", "POST"])
def index():
    app.logger.debug("Rendering index page with method %s", request.method)

    if request.method == "POST":
        uploaded_file = request.files.get("video")
        parts = request.form.get("parts", type=int)

        app.logger.info(
            "Received upload request: filename=%s parts=%s", getattr(uploaded_file, "filename", None), parts
        )

        if uploaded_file is None or uploaded_file.filename == "":
            flash("Please choose an MP4 file to upload.", "error")
            return redirect(request.url)

        if not allowed_file(uploaded_file.filename):
            flash("Only MP4 files are supported.", "error")
            return redirect(request.url)

        if parts is None or parts < MIN_PARTS or parts > MAX_PARTS:
            flash("Please choose between 2 and 4 parts.", "error")
            return redirect(request.url)

        job_id = uuid.uuid4().hex
        upload_dir = UPLOAD_FOLDER / job_id
        output_dir = OUTPUT_FOLDER / job_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        filename = secure_filename(uploaded_file.filename)
        saved_file = upload_dir / filename
        app.logger.info("Saving uploaded file to %s", saved_file)
        uploaded_file.save(saved_file)

        base_metadata = {
            "job_id": job_id,
            "original_filename": filename,
            "parts": parts,
            "status": "processing",
            "created_at": iso_now(),
            "outputs": [],
        }
        save_metadata(output_dir, base_metadata)

        EXECUTOR.submit(process_job, job_id, saved_file, output_dir, parts, base_metadata)

        flash("Your video is being processed. The page will update once it is ready.", "info")
        return redirect(url_for("result", job_id=job_id))

    return render_template("index.html")


@app.route("/result/<job_id>")
def result(job_id: str):
    output_dir = OUTPUT_FOLDER / job_id
    if not output_dir.exists():
        flash("The requested result was not found.", "error")
        return redirect(url_for("index"))

    payload = build_job_payload(job_id, output_dir)
    metadata = payload.get("metadata", {})
    status = payload.get("status", "unknown")
    output_files = payload.get("files", [])

    if status == "error":
        app.logger.error(
            "Job %s is in error state: %s", job_id, metadata.get("error_message")
        )

    app.logger.info(
        "Rendering result for job %s with status %s and %d files",
        job_id,
        status,
        len(output_files),
    )
    return render_template(
        "result.html",
        job_id=job_id,
        payload=payload,
        metadata=metadata,
        status=status,
        files=output_files,
    )


@app.route("/download/<job_id>/<path:filename>")
def download(job_id: str, filename: str):
    output_dir = OUTPUT_FOLDER / job_id
    if not output_dir.exists():
        flash("The requested file was not found.", "error")
        return redirect(url_for("index"))

    if Path(filename).name != filename:
        abort(400)

    app.logger.info("Downloading %s from job %s", filename, job_id)
    return send_from_directory(output_dir, filename, as_attachment=True)


@app.route("/status/<job_id>")
def job_status(job_id: str):
    output_dir = OUTPUT_FOLDER / job_id
    if not output_dir.exists():
        app.logger.warning("Status requested for missing job %s", job_id)
        return jsonify({"job_id": job_id, "status": "not-found"}), 404

    payload = build_job_payload(job_id, output_dir)
    app.logger.debug(
        "Returning status for job %s: %s with %d files",
        job_id,
        payload.get("status"),
        len(payload.get("files", [])),
    )
    return jsonify(payload)


@app.before_request
def log_request_start():
    app.logger.debug("Handling %s %s", request.method, request.path)


@app.after_request
def log_request_end(response):
    app.logger.debug(
        "Completed %s %s with status %s", request.method, request.path, response.status_code
    )
    return response


# Solo para desarrollo local, no en producción con Gunicorn
def run_dev():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
