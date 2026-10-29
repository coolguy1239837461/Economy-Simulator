#!/usr/bin/env python3
import http.server
import socketserver
import urllib.parse
import urllib.error
import urllib.request
import json
import os
import re
import tempfile
import mimetypes
import io
import shutil
import stat
import zipfile
import zlib
import subprocess
import sys
from pathlib import Path, PurePosixPath

PORT = 8000
ROBLOX_DOMAIN_SUFFIXES = ("roblox.com", "rbxcdn.com")
APP_DIRECTORY = Path(__file__).resolve().parent
ONLINE_ASSET_FALLBACK = os.environ.get("AYA_ONLINE_ASSETS", "1").strip().lower() not in {
    "0", "false", "no", "off",
}


def find_intercept_asset_directories():
    directories = [APP_DIRECTORY / "InterceptAssets"]
    game_roots = (
        APP_DIRECTORY / "game" / "games",
        APP_DIRECTORY / "games",
    )
    for game_root in game_roots:
        directories.append(game_root / "InterceptAssets")
        try:
            directories.extend(
                child / "InterceptAssets"
                for child in sorted(game_root.iterdir())
                if child.is_dir() and not child.is_symlink()
            )
        except FileNotFoundError:
            continue
    return [directory for directory in directories if directory.is_dir()]


def build_intercept_asset_index():
    index = {}
    for directory in find_intercept_asset_directories():
        for asset_path in sorted(directory.iterdir()):
            if asset_path.is_symlink() or not asset_path.is_file():
                continue
            asset_id = asset_path.stem
            if asset_id.isdigit():
                index.setdefault(asset_id, asset_path)
    return index


INTERCEPT_ASSETS = build_intercept_asset_index()
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_PLACE_BYTES = 1024 * 1024 * 1024
PLACE_UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_ZIP_ENTRIES = 20000
MAX_ZIP_EXTRACTED_BYTES = 2 * MAX_PLACE_BYTES
MAX_REPORT_BYTES = 5 * 1024 * 1024
CONVERTER_DIRECTORY = APP_DIRECTORY / "tools" / "rbx-place-converter"
CONVERTER_BINARY = Path(os.environ.get(
    "AYA_RBX_CONVERTER",
    str(CONVERTER_DIRECTORY / "target" / "release" / (
        "rbx-place-converter.exe" if os.name == "nt" else "rbx-place-converter"
    )),
))


def get_requested_asset_id(query):
    for key in ("id", "assetversionid"):
        values = query.get(key, [])
        if values and values[0].isdigit():
            return values[0]
    return None


def safe_game_name(value):
    name = Path(str(value or "Place").replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return (name[:100] or "Place")


def get_game_directory(value):
    return APP_DIRECTORY / "game" / "games" / safe_game_name(value)


def safe_zip_member_path(name):
    if not name or "\x00" in name or "\\" in name:
        return None
    member_path = PurePosixPath(name)
    if member_path.is_absolute() or any(part in ("", ".", "..") for part in member_path.parts):
        return None
    return member_path


def extract_play_zip(zip_path, game_directory):
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(f"Could not read ZIP file: {error}") from error

    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise ValueError(f"ZIP contains too many entries (maximum {MAX_ZIP_ENTRIES})")
        if sum(entry.file_size for entry in entries) > MAX_ZIP_EXTRACTED_BYTES:
            raise ValueError("ZIP expands beyond the 2 GiB extraction limit")

        places = []
        assets = []
        for entry in entries:
            member_path = safe_zip_member_path(entry.filename)
            if member_path is None:
                raise ValueError(f"ZIP contains an unsafe path: {entry.filename[:160]}")
            if entry.is_dir():
                continue
            if entry.flag_bits & 0x1:
                raise ValueError("Encrypted ZIP files are not supported")
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"ZIP contains a symbolic link: {entry.filename[:160]}")

            suffix = member_path.suffix.lower()
            if suffix in (".rbxl", ".rbxlx"):
                if entry.file_size > MAX_PLACE_BYTES:
                    raise ValueError(f"Place file is too large: {entry.filename[:160]}")
                places.append((entry, member_path))
                continue

            parts = member_path.parts
            if len(parts) >= 2 and parts[-2].casefold() == "interceptassets":
                asset_name = parts[-1]
                asset_path = PurePosixPath(asset_name)
                asset_id = asset_path.stem
                extension = asset_path.suffix.lower().lstrip(".")
                if asset_id.isdigit() and re.fullmatch(r"[a-z0-9]{1,8}", extension):
                    assets.append((entry, asset_id, extension))

        if not places:
            raise ValueError("ZIP does not contain a .rbxl or .rbxlx place file")

        stored_place_names = [re.sub(r"[^A-Za-z0-9._ -]", "_", path.name)[:120] for _, path in places]
        if any(not name for name in stored_place_names):
            raise ValueError("ZIP contains a place with an invalid filename")
        if len({name.casefold() for name in stored_place_names}) != len(stored_place_names):
            raise ValueError("ZIP contains place files with duplicate filenames")
        asset_ids = [asset_id for _, asset_id, _ in assets]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("ZIP contains duplicate asset IDs in InterceptAssets")

        extracted_size = sum(entry.file_size for entry, _ in places)
        extracted_size += sum(entry.file_size for entry, _, _ in assets)
        if extracted_size > MAX_ZIP_EXTRACTED_BYTES:
            raise ValueError("Place and asset files expand beyond the 2 GiB extraction limit")

        game_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".play-stage-", dir=game_directory) as staging_name:
            staging = Path(staging_name)
            staged_places = staging / "Places"
            staged_assets = staging / "InterceptAssets"
            staged_places.mkdir()
            if assets:
                staged_assets.mkdir()

            def extract_entry(entry, destination):
                written = 0
                with archive.open(entry) as source, destination.open("xb") as target:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > entry.file_size or written > MAX_PLACE_BYTES:
                            raise ValueError(f"ZIP entry exceeded its declared size: {entry.filename[:160]}")
                        target.write(chunk)
                if written != entry.file_size:
                    raise ValueError(f"ZIP entry size did not match its header: {entry.filename[:160]}")

            try:
                for (entry, _), stored_name in zip(places, stored_place_names):
                    extract_entry(entry, staged_places / stored_name)
                for entry, asset_id, extension in assets:
                    extract_entry(entry, staged_assets / f"{asset_id}.{extension}")
            except (EOFError, NotImplementedError, OSError, RuntimeError, zipfile.BadZipFile, zlib.error) as error:
                raise ValueError(f"Could not extract ZIP contents: {error}") from error

            places_directory = game_directory / "Places"
            places_directory.mkdir(exist_ok=True)
            for stored_name in stored_place_names:
                os.replace(staged_places / stored_name, places_directory / stored_name)

            if assets:
                assets_directory = game_directory / "InterceptAssets"
                assets_directory.mkdir(exist_ok=True)
                for _, asset_id, extension in assets:
                    asset_path = assets_directory / f"{asset_id}.{extension}"
                    os.replace(staged_assets / asset_path.name, asset_path)
                    INTERCEPT_ASSETS[asset_id] = asset_path

        return {
            "places": [
                {"name": entry.filename, "file": stored_name, "size": entry.file_size}
                for (entry, _), stored_name in zip(places, stored_place_names)
            ],
            "assets": len(assets),
        }


def infer_asset_extension(body, content_type, final_url):
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if body.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "webp"
    if body.startswith(b"RIFF") and body[8:12] == b"WAVE":
        return "wav"
    if body.startswith(b"OggS"):
        return "ogg"
    if body.startswith(b"ID3") or body.startswith(b"\xff\xfb"):
        return "mp3"
    if body.startswith(b"fLaC"):
        return "flac"
    if body.startswith(b"PK\x03\x04"):
        return "zip"

    extension = Path(urllib.parse.urlparse(final_url).path).suffix.lower().lstrip(".")
    if re.fullmatch(r"[a-z0-9]{2,8}", extension):
        return extension
    extension = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ".bin"
    return extension.lstrip(".")

# Headers required for Emscripten multithreading (COOP + COEP)
EM_HEADERS = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
    # helpful for loading cross-origin resources; adjust as needed
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

ADMINS = {
    232,
    71217160,
    3174711,
    1337617,
    170263,
    38542771,
    391038733,
    115,
    8243709,
    2723542,
    9009123,
}


def is_roblox_hostname(hostname):
    hostname = (hostname or "").lower().rstrip(".")
    return any(
        hostname == suffix or hostname.endswith("." + suffix)
        for suffix in ROBLOX_DOMAIN_SUFFIXES
    )


class RobloxAssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        target = urllib.parse.urlparse(new_url)
        if target.scheme != "https" or not is_roblox_hostname(target.hostname):
            return None
        return super().redirect_request(request, response, code, message, headers, new_url)


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        for k, v in EM_HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def request_is_same_origin(self):
        origin = urllib.parse.urlparse(self.headers.get("Origin", ""))
        return bool(origin.netloc and origin.netloc.lower() == self.headers.get("Host", "").lower())

    def read_request_body(self, limit):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_error(411, "A valid Content-Length header is required")
            return None
        if length < 0 or length > limit:
            self.send_error(413, "Request body is too large")
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self.send_error(400, "Request body was truncated")
            return None
        return body

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not (
            parsed.path.startswith("/api/intercept-assets/")
            or parsed.path == "/api/play/upload-zip-chunk"
        ):
            self.send_error(404, "Unknown API endpoint")
            return
        if not self.request_is_same_origin():
            self.send_error(403, "InterceptAssets API requests must be same-origin")
            return

        if parsed.path == "/api/intercept-assets/save-asset":
            asset_ids = query.get("id", [])
            if not asset_ids or not asset_ids[0].isdigit():
                self.send_error(400, "A numeric asset ID is required")
                return
            body = self.read_request_body(MAX_UPLOAD_BYTES)
            if body is None:
                return
            if not body:
                self.send_error(400, "Asset response is empty")
                return

            game_name = safe_game_name(query.get("place", ["Place"])[0])
            asset_directory = get_game_directory(game_name) / "InterceptAssets"
            asset_directory.mkdir(parents=True, exist_ok=True)
            content_type = self.headers.get("X-Asset-Content-Type", "application/octet-stream")
            final_url = self.headers.get("X-Asset-Source-Url", "")
            contexts = []
            try:
                contexts = json.loads(self.headers.get("X-Asset-Contexts", "[]"))
            except json.JSONDecodeError:
                self.send_error(400, "Asset context metadata must be valid JSON")
                return
            if not isinstance(contexts, list) or len(contexts) > 500:
                self.send_error(400, "Asset context metadata is invalid")
                return
            safe_contexts = []
            for context in contexts:
                if not isinstance(context, dict):
                    continue
                safe_contexts.append({
                    key: str(context.get(key, ""))[:1000]
                    for key in ("type", "name", "path", "property")
                })

            extension = infer_asset_extension(body, content_type, final_url)
            asset_id = asset_ids[0]
            asset_path = asset_directory / f"{asset_id}.{extension}"
            asset_path.write_bytes(body)

            manifest_path = asset_directory / "manifest.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    self.send_error(500, "Existing InterceptAssets manifest is unreadable")
                    return
            else:
                manifest = {}
            manifest[asset_id] = {
                "file": asset_path.name,
                "contentType": content_type,
                "contexts": safe_contexts,
            }
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            INTERCEPT_ASSETS[asset_id] = asset_path
            self.send_json(200, {"id": asset_id, "file": asset_path.name, "size": len(body)})
            return

        if parsed.path == "/api/play/upload-zip-chunk":
            names = query.get("name", [])
            upload_ids = query.get("upload", [])
            offsets = query.get("offset", [])
            totals = query.get("total", [])
            if not names or not upload_ids or not offsets or not totals:
                self.send_error(400, "ZIP filename and upload metadata are required")
                return
            upload_id = upload_ids[0]
            if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
                self.send_error(400, "ZIP upload ID is invalid")
                return
            try:
                offset = int(offsets[0])
                total = int(totals[0])
                chunk_length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_error(400, "ZIP upload sizes must be valid integers")
                return
            if total <= 0 or total > MAX_PLACE_BYTES or offset < 0 or offset >= total:
                self.send_error(413 if total > MAX_PLACE_BYTES else 400, "ZIP upload size or offset is invalid")
                return
            if chunk_length <= 0 or chunk_length > PLACE_UPLOAD_CHUNK_BYTES:
                self.send_error(413, "ZIP upload chunk is too large")
                return
            if offset + chunk_length > total:
                self.send_error(400, "ZIP upload chunk exceeds the declared file size")
                return

            requested_filename = str(names[0]).replace("\\", "/").rsplit("/", 1)[-1]
            filename = re.sub(r"[^A-Za-z0-9._ -]", "_", requested_filename)[:120]
            if not re.fullmatch(r".+\.zip", filename, re.IGNORECASE):
                self.send_error(400, "Choose a .zip archive")
                return

            body = self.read_request_body(PLACE_UPLOAD_CHUNK_BYTES)
            if body is None:
                return
            if len(body) != chunk_length:
                self.send_error(400, "ZIP upload chunk was truncated")
                return

            game_name = safe_game_name(Path(filename).stem)
            game_directory = get_game_directory(game_name)
            game_directory.mkdir(parents=True, exist_ok=True)
            temporary_path = game_directory / f".play-zip-{upload_id}.tmp"
            if temporary_path.is_symlink():
                self.send_error(400, "ZIP upload path is invalid")
                return
            is_final_chunk = False
            try:
                if offset == 0:
                    with temporary_path.open("xb") as temporary_file:
                        temporary_file.write(body)
                else:
                    if not temporary_path.is_file() or temporary_path.stat().st_size != offset:
                        self.send_error(409, "ZIP upload offset does not match the saved data")
                        return
                    with temporary_path.open("ab") as temporary_file:
                        temporary_file.write(body)
                received = offset + len(body)
                complete = received == total
                is_final_chunk = complete
                if not complete:
                    self.send_json(200, {
                        "received": received,
                        "total": total,
                        "complete": False,
                    })
                    return

                try:
                    contents = extract_play_zip(temporary_path, game_directory)
                except ValueError as error:
                    self.send_json(422, {"error": str(error)})
                    return
                self.send_json(200, {
                    "game": game_name,
                    "received": received,
                    "total": total,
                    "complete": True,
                    **contents,
                })
            except FileExistsError:
                self.send_error(409, "ZIP upload ID is already in use")
                return
            except OSError as error:
                self.send_error(500, f"Could not save ZIP upload: {error}")
                return
            finally:
                if is_final_chunk and temporary_path.exists():
                    temporary_path.unlink()
            return

        if parsed.path == "/api/intercept-assets/save-place-chunk":
            names = query.get("name", [])
            upload_ids = query.get("upload", [])
            offsets = query.get("offset", [])
            totals = query.get("total", [])
            if not names or not upload_ids or not offsets or not totals:
                self.send_error(400, "Place filename and upload metadata are required")
                return
            upload_id = upload_ids[0]
            if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
                self.send_error(400, "Place upload ID is invalid")
                return
            try:
                offset = int(offsets[0])
                total = int(totals[0])
                chunk_length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_error(400, "Place upload sizes must be valid integers")
                return
            if total <= 0 or total > MAX_PLACE_BYTES or offset < 0 or offset >= total:
                self.send_error(413 if total > MAX_PLACE_BYTES else 400, "Place upload size or offset is invalid")
                return
            if chunk_length <= 0 or chunk_length > PLACE_UPLOAD_CHUNK_BYTES:
                self.send_error(413, "Place upload chunk is too large")
                return
            if offset + chunk_length > total:
                self.send_error(400, "Place upload chunk exceeds the declared file size")
                return

            requested_filename = str(names[0]).replace("\\", "/").rsplit("/", 1)[-1]
            filename = re.sub(r"[^A-Za-z0-9._ -]", "_", requested_filename)[:120]
            if not re.fullmatch(r".+\.(rbxlx|rbxl)", filename, re.IGNORECASE):
                self.send_error(400, "Only .rbxlx and .rbxl place files can be saved")
                return

            body = self.read_request_body(PLACE_UPLOAD_CHUNK_BYTES)
            if body is None:
                return
            if len(body) != chunk_length:
                self.send_error(400, "Place upload chunk was truncated")
                return

            game_name = safe_game_name(Path(filename).stem)
            game_directory = get_game_directory(game_name)
            game_directory.mkdir(parents=True, exist_ok=True)
            temporary_path = game_directory / f".place-upload-{upload_id}"
            if temporary_path.is_symlink():
                self.send_error(400, "Place upload path is invalid")
                return
            try:
                if offset == 0:
                    with temporary_path.open("xb") as temporary_file:
                        temporary_file.write(body)
                else:
                    if not temporary_path.is_file() or temporary_path.stat().st_size != offset:
                        self.send_error(409, "Place upload offset does not match the saved data")
                        return
                    with temporary_path.open("ab") as temporary_file:
                        temporary_file.write(body)
                received = offset + len(body)
                complete = received == total
                if complete:
                    os.replace(temporary_path, game_directory / filename)
            except FileExistsError:
                self.send_error(409, "Place upload ID is already in use")
                return
            except OSError as error:
                self.send_error(500, f"Could not save place upload chunk: {error}")
                return
            self.send_json(200, {
                "place": filename,
                "received": received,
                "total": total,
                "complete": complete,
            })
            return

        if parsed.path == "/api/intercept-assets/save-place":
            names = query.get("name", [])
            if not names:
                self.send_error(400, "A place filename is required")
                return
            requested_filename = str(names[0]).replace("\\", "/").rsplit("/", 1)[-1]
            filename = re.sub(r"[^A-Za-z0-9._ -]", "_", requested_filename)[:120]
            if not re.fullmatch(r".+\.(rbxlx|rbxl)", filename, re.IGNORECASE):
                self.send_error(400, "Only .rbxlx and .rbxl place files can be saved")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_error(411, "A valid Content-Length header is required")
                return
            if length < 0:
                self.send_error(400, "Place file size is invalid")
                return
            if length > MAX_PLACE_BYTES:
                self.send_error(413, "Place file is too large (maximum 1 GiB)")
                return
            if length == 0:
                self.send_error(400, "Place file is empty")
                return
            game_name = safe_game_name(Path(filename).stem)
            game_directory = get_game_directory(game_name)
            game_directory.mkdir(parents=True, exist_ok=True)
            destination = game_directory / filename
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=game_directory, prefix=".place-upload-", delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            self.send_error(400, "Place upload was truncated")
                            return
                        temporary_file.write(chunk)
                        remaining -= len(chunk)
                os.replace(temporary_path, destination)
            except OSError as error:
                self.send_error(500, f"Could not save place file: {error}")
                return
            finally:
                if temporary_path is not None and temporary_path.exists():
                    temporary_path.unlink()
            self.send_json(200, {"place": destination.name, "game": game_name, "size": length})
            return

        if parsed.path == "/api/intercept-assets/convert-place":
            body = self.read_request_body(MAX_UPLOAD_BYTES)
            if body is None:
                return
            if not body:
                self.send_error(400, "Place file is empty")
                return
            xml_candidate = body.lstrip(b"\xef\xbb\xbf \t\r\n")
            is_xml = xml_candidate.startswith(b"<?xml") or re.match(
                rb"<roblox(?:\s|>)", xml_candidate,
            ) is not None
            if not is_xml:
                if not CONVERTER_BINARY.is_file():
                    self.send_json(503, {
                        "error": "Binary .rbxl conversion is unavailable. Build the bundled rbx-dom helper or set AYA_RBX_CONVERTER.",
                    })
                    return
                try:
                    conversion = subprocess.run(
                        [str(CONVERTER_BINARY)],
                        input=body,
                        capture_output=True,
                        timeout=120,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    self.send_error(504, "Binary place conversion timed out")
                    return
                except OSError as error:
                    self.send_error(500, f"Could not start binary place converter: {error}")
                    return
                if conversion.returncode != 0:
                    message = conversion.stderr.decode("utf-8", errors="replace")[-2000:]
                    self.send_json(422, {"error": f"Could not parse binary place: {message}"})
                    return
                body = conversion.stdout
                if not body.startswith(b"<?xml") and not body.startswith(b"<roblox"):
                    self.send_error(500, "Binary place converter returned invalid XML")
                    return
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/intercept-assets/save-report":
            body = self.read_request_body(MAX_REPORT_BYTES)
            if body is None:
                return
            try:
                report = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400, "Report must be valid UTF-8 JSON")
                return
            if not isinstance(report, dict):
                self.send_error(400, "Report must be a JSON object")
                return
            game_name = safe_game_name(query.get("place", ["Place"])[0])
            asset_directory = get_game_directory(game_name) / "InterceptAssets"
            asset_directory.mkdir(parents=True, exist_ok=True)
            report_path = asset_directory / "asset-report.json"
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.send_json(200, {"report": report_path.name})
            return

        self.send_error(404, "Unknown API endpoint")

    def do_OPTIONS(self):
        # respond to preflight
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if self.request_is_same_origin():
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()

    def serve_intercept_asset(self, asset_id):
        asset_path = INTERCEPT_ASSETS.get(asset_id)
        if asset_path is None:
            return False

        try:
            resolved_path = asset_path.resolve(strict=True)
            if not any(
                resolved_path.is_relative_to(directory.resolve())
                for directory in find_intercept_asset_directories()
            ):
                return False
            file_size = resolved_path.stat().st_size
            range_header = self.headers.get("Range")
            start = 0
            end = file_size - 1
            status = 200
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                if not match or (not match.group(1) and not match.group(2)) or file_size == 0:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return True
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), file_size - 1) if last else file_size - 1
                else:
                    suffix_length = int(last)
                    start = max(file_size - suffix_length, 0)
                if start >= file_size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return True
                status = 206

            content_length = end - start + 1
            content_type = mimetypes.guess_type(resolved_path.name)[0] or "application/octet-stream"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("Content-Length", str(content_length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.end_headers()
            with resolved_path.open("rb") as asset_file:
                asset_file.seek(start)
                remaining = content_length
                while remaining:
                    chunk = asset_file.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (OSError, ValueError):
            self.send_error(404, "Local intercepted asset is unavailable")
        return True

    def asset_not_available_offline(self):
        self.send_error(404, "Asset is not in InterceptAssets and online Roblox fallback is disabled")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("", "/"):
            self.path = "/welcome.html"
            if parsed.query:
                self.path += "?" + parsed.query
            return super().do_GET()

        if path == "/api/play/place":
            games = qs.get("game", [])
            filenames = qs.get("file", [])
            if not games or not filenames:
                self.send_error(400, "A game and place filename are required")
                return
            filename = filenames[0]
            if not re.fullmatch(r"[A-Za-z0-9._ -]{1,120}\.(rbxlx|rbxl)", filename, re.IGNORECASE):
                self.send_error(400, "Place filename is invalid")
                return
            places_directory = get_game_directory(games[0]) / "Places"
            place_path = places_directory / filename
            try:
                resolved_directory = places_directory.resolve(strict=True)
                resolved_path = place_path.resolve(strict=True)
                if resolved_path.parent != resolved_directory or not resolved_path.is_file():
                    raise OSError("Place file is outside the package directory")
                file_size = resolved_path.stat().st_size
            except OSError:
                self.send_error(404, "Place file was not found in the ZIP package")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with resolved_path.open("rb") as place_file:
                shutil.copyfileobj(place_file, self.wfile, length=1024 * 1024)
            return

        if path == "/api/intercept-assets/archive":
            places = qs.get("place", [])
            if not places:
                self.send_error(400, "A place name is required")
                return
            game_name = safe_game_name(places[0])
            game_directory = get_game_directory(game_name)
            if not game_directory.is_dir():
                self.send_error(404, "Converted place package was not found")
                return
            with tempfile.TemporaryFile(mode="w+b") as archive:
                try:
                    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
                        for file_path in sorted(game_directory.rglob("*")):
                            if file_path.is_symlink() or not file_path.is_file():
                                continue
                            package.write(file_path, file_path.relative_to(game_directory.parent))
                except (OSError, zipfile.LargeZipFile) as error:
                    self.send_error(500, f"Could not create package archive: {error}")
                    return
                archive.seek(0, io.SEEK_END)
                archive_size = archive.tell()
                archive.seek(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{game_name}-offline-assets.zip"')
                self.send_header("Content-Length", str(archive_size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while True:
                    chunk = archive.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        if path.startswith("/RobloxProxy/"):
            proxy_target = path[len("/RobloxProxy/"):]
            hostname, separator, asset_path = proxy_target.partition("/")
            hostname = hostname.lower().rstrip(".")
            asset_path = "/" + asset_path if separator else ""
            lowered_path = asset_path.lower()
            is_asset_path = (
                hostname == "rbxcdn.com"
                or hostname.endswith(".rbxcdn.com")
                or lowered_path == "/asset"
                or lowered_path.startswith(("/asset/", "/asset.", "/v1/asset", "/v1/assets/"))
                or lowered_path.startswith(("/images/", "/thumbs/", "/thumbnail"))
            )
            if not is_roblox_hostname(hostname) or not is_asset_path or re.search(r"[\r\n]", asset_path):
                self.send_error(400, "Only Roblox asset and CDN URLs can be proxied")
                return

            asset_id = get_requested_asset_id(qs)
            if asset_id and self.serve_intercept_asset(asset_id):
                return
            if not ONLINE_ASSET_FALLBACK:
                self.asset_not_available_offline()
                return

            is_legacy_asset_url = (
                hostname in ("www.roblox.com", "assetgame.roblox.com")
                and lowered_path.rstrip("/") == "/asset"
            )
            if is_legacy_asset_url:
                asset_ids = qs.get("id", [])
                asset_version_ids = qs.get("assetversionid", [])
                if asset_ids and asset_ids[0].isdigit():
                    delivery_query = {"id": asset_ids[0]}
                    versions = qs.get("version", [])
                    if versions and versions[0].isdigit():
                        delivery_query["version"] = versions[0]
                    delivery_query_string = urllib.parse.urlencode(delivery_query)
                    asset_url = f"https://assetdelivery.roblox.com/v1/asset?{delivery_query_string}"
                elif asset_version_ids and asset_version_ids[0].isdigit():
                    delivery_query = urllib.parse.urlencode({"id": asset_version_ids[0]})
                    asset_url = f"https://assetdelivery.roblox.com/v1/assetVersion?{delivery_query}"
                else:
                    self.send_error(400, "A numeric Roblox asset id or asset version id is required")
                    return
            else:
                asset_url = urllib.parse.urlunparse((
                    "https",
                    hostname,
                    asset_path,
                    "",
                    parsed.query,
                    "",
                ))
            request_headers = {"User-Agent": "Aya-WASM-Studio"}
            if self.headers.get("Range"):
                request_headers["Range"] = self.headers["Range"]
            request = urllib.request.Request(asset_url, headers=request_headers)
            try:
                opener = urllib.request.build_opener(RobloxAssetRedirectHandler)
                with opener.open(request, timeout=30) as response:
                    body = response.read()
                    self.send_response(response.status)
                    self.send_header(
                        "Content-Type",
                        response.headers.get("Content-Type", "application/octet-stream"),
                    )
                    self.send_header("Cache-Control", "public, max-age=3600")
                    for header in ("Content-Range", "Accept-Ranges"):
                        if response.headers.get(header):
                            self.send_header(header, response.headers[header])
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            except urllib.error.HTTPError as error:
                self.send_error(error.code, f"Roblox asset delivery returned HTTP {error.code}")
            except urllib.error.URLError as error:
                self.send_error(502, f"Could not reach Roblox asset delivery: {error.reason}")
            return

        if path.rstrip("/").lower() == "/asset":
            asset_id = get_requested_asset_id(qs)
            if not asset_id:
                self.send_error(400, "A numeric Roblox asset id or asset version id is required")
                return

            if self.serve_intercept_asset(asset_id):
                return
            if not ONLINE_ASSET_FALLBACK:
                self.asset_not_available_offline()
                return

            if qs.get("assetversionid", []) and not qs.get("id", []):
                asset_url = f"https://assetdelivery.roblox.com/v1/assetVersion?id={asset_id}"
            else:
                asset_url = f"https://assetdelivery.roblox.com/v1/asset?id={asset_id}"

            request = urllib.request.Request(
                asset_url,
                headers={"User-Agent": "Aya-WASM-Studio"},
            )
            try:
                opener = urllib.request.build_opener(RobloxAssetRedirectHandler)
                with opener.open(request, timeout=30) as response:
                    body = response.read()
                    self.send_response(response.status)
                    self.send_header(
                        "Content-Type",
                        response.headers.get("Content-Type", "application/octet-stream"),
                    )
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            except urllib.error.HTTPError as error:
                self.send_error(error.code, f"Roblox asset delivery returned HTTP {error.code}")
            except urllib.error.URLError as error:
                self.send_error(502, f"Could not reach Roblox asset delivery: {error.reason}")
            return

        # /userblock/getblockedusers -> return empty JSON object
        if "/userblock/getblockedusers" in path:
            body = "{}".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # /game/LuaWebService/HandleSocialRequest.ashx
        if "/game/LuaWebService/HandleSocialRequest.ashx" in path:
            # Set Content-Type as in original PHP: text/html; charset=utf-8
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # disable caching to ease development
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            # will fill body below
            # parse helper
            def g(name, default=""):
                v = qs.get(name)
                return v[0] if v else default

            method = g("method")
            body_text = ""

            if method == "IsBestFriendsWith":
                body_text = '<Value Type="boolean">false</Value>'
            elif method == "IsFriendsWith":
                body_text = '<Value Type="boolean">false</Value>'
            elif method == "IsInGroup":
                groupid = g("groupid")
                playerid = g("playerid")
                value = "false"
                if groupid == "1200769":
                    try:
                        if int(playerid) in ADMINS:
                            value = "true"
                    except Exception:
                        value = "false"
                body_text = f'<Value Type="boolean">{value}</Value>'
            elif method == "GetGroupRank":
                groupid = g("groupid")
                playerid = g("playerid")
                value = "false"
                if groupid == "1200769":
                    try:
                        if int(playerid) in ADMINS:
                            value = "true"
                    except Exception:
                        value = "false"
                body_text = f'<Value Type="boolean">{value}</Value>'
            else:
                # default: empty response (200)
                body_text = ""

            body = body_text.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
            return
        
        if "/Game/LuaWebService/HandleSocialRequest.ashx" in path:
            # Set Content-Type as in original PHP: text/html; charset=utf-8
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # disable caching to ease development
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            # will fill body below
            # parse helper
            def g(name, default=""):
                v = qs.get(name)
                return v[0] if v else default

            method = g("method")
            body_text = ""

            if method == "IsBestFriendsWith":
                body_text = '<Value Type="boolean">false</Value>'
            elif method == "IsFriendsWith":
                body_text = '<Value Type="boolean">false</Value>'
            elif method == "IsInGroup":
                groupid = g("groupid")
                playerid = g("playerid")
                value = "false"
                if groupid == "1200769":
                    try:
                        if int(playerid) in ADMINS:
                            value = "true"
                    except Exception:
                        value = "false"
                body_text = f'<Value Type="boolean">{value}</Value>'
            elif method == "GetGroupRank":
                groupid = g("groupid")
                playerid = g("playerid")
                value = "false"
                if groupid == "1200769":
                    try:
                        if int(playerid) in ADMINS:
                            value = "true"
                    except Exception:
                        value = "false"
                body_text = f'<Value Type="integer">255</Value>'
            else:
                # default: empty response (200)
                body_text = ""

            body = body_text.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
            return

        # fallback to static file serving from current directory
        return super().do_GET()


if __name__ == "__main__":
    class ThreadingServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with ThreadingServer(("", PORT), Handler) as httpd:
        print(f"Serving HTTP on 0.0.0.0 port {PORT} (http://0.0.0.0:{PORT}/) ...")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down")
            httpd.server_close()
