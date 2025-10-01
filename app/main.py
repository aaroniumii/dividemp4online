import json
import os
import subprocess
import uuid
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
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

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["OUTPUT_FOLDER"] = str(OUTPUT_FOLDER)


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def run_command(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


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
    result = run_command(command)
    return float(result.stdout.strip())


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
        run_command(command)
        output_files.append(output_file)

    return output_files


def save_metadata(output_dir: Path, data: dict) -> None:
    metadata_path = output_dir / METADATA_FILENAME
    with metadata_path.open("w", encoding="utf-8") as metadata_file:
        json.dump(data, metadata_file, indent=2)


def load_metadata(output_dir: Path) -> dict | None:
    metadata_path = output_dir / METADATA_FILENAME
    if not metadata_path.exists():
        return None
    with metadata_path.open(encoding="utf-8") as metadata_file:
        return json.load(metadata_file)


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        uploaded_file = request.files.get("video")
        parts = request.form.get("parts", type=int)

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
        uploaded_file.save(saved_file)

        try:
            output_files = split_video(saved_file, output_dir, parts)
        except (subprocess.CalledProcessError, ValueError) as error:
            if isinstance(error, subprocess.CalledProcessError):
                app.logger.error("Error splitting video: %s", error.stderr)
            else:
                app.logger.error("Error determining video duration: %s", error)
            flash("There was a problem processing the video. Please try again.", "error")
            return redirect(request.url)

        save_metadata(
            output_dir,
            {
                "original_filename": filename,
                "parts": parts,
                "outputs": [file.name for file in output_files],
            },
        )

        return redirect(url_for("result", job_id=job_id))

    return render_template("index.html")


@app.route("/result/<job_id>")
def result(job_id: str):
    output_dir = OUTPUT_FOLDER / job_id
    if not output_dir.exists():
        flash("The requested result was not found.", "error")
        return redirect(url_for("index"))

    metadata = load_metadata(output_dir) or {}
    output_files = [file for file in output_dir.iterdir() if file.is_file() and file.name != METADATA_FILENAME]
    output_files.sort()

    return render_template(
        "result.html",
        job_id=job_id,
        metadata=metadata,
        files=[file.name for file in output_files],
    )


@app.route("/download/<job_id>/<path:filename>")
def download(job_id: str, filename: str):
    output_dir = OUTPUT_FOLDER / job_id
    if not output_dir.exists():
        flash("The requested file was not found.", "error")
        return redirect(url_for("index"))

    if Path(filename).name != filename:
        abort(400)

    return send_from_directory(output_dir, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
