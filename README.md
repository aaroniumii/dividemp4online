# Divide MP4 Online

A lightweight web interface that lets you split MP4 videos into 2, 3, or 4 equal parts using FFmpeg. Upload a video from your browser, let the server process it, and download the resulting clips.

## Features

- Upload MP4 files directly from the browser.
- Choose between 2, 3, or 4 equal-duration segments.
- Download the generated segments individually.
- Runs locally on your own infrastructure with Docker Compose.

## Requirements

- [Docker](https://docs.docker.com/get-docker/) 20.10+
- [Docker Compose](https://docs.docker.com/compose/) v2+

## Quick start

1. Clone the repository and enter the project folder:

   ```bash
   git clone https://github.com/your-user/dividemp4online.git
   cd dividemp4online
   ```

2. Start the service:

   ```bash
   docker compose up --build
   ```

3. Open your browser at [http://localhost:8000](http://localhost:8000) and upload an MP4 file. Choose how many parts you want and wait until the download links appear.

4. The processed files are stored under the `data/` directory on the host machine. You can delete the folder when you no longer need the files.

## Configuration

| Environment variable | Description                                   | Default     |
| -------------------- | --------------------------------------------- | ----------- |
| `SECRET_KEY`         | Secret key used for signing Flask sessions.   | `change-me` |

To set a custom value, export the variable before starting Docker Compose:

```bash
export SECRET_KEY="your-secret"
docker compose up --build
```

## Development

To run the application without Docker:

1. Make sure Python 3.11+ and FFmpeg are installed on your machine.
2. Install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Start the server:

   ```bash
   python app/main.py
   ```

4. Visit [http://localhost:8000](http://localhost:8000).

Uploaded files and generated segments are stored in `data/uploads/` and `data/outputs/` respectively.

## License

This project is released under the MIT License.
