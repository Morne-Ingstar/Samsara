"""One reviewed, immutable ZIP per send. No raw logs, audio or automatic retries."""
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from samsara.support_feedback import REPORT_UPLOAD_URL, build_safe_diagnostic_summary

MAX_BUNDLE_BYTES = 8_388_608
UPLOAD_TIMEOUT = 20
BUILD_CONFIG = Path(__file__).resolve().parents[1] / "data" / "report_upload.json"


@dataclass(frozen=True)
class UploadSettings:
    token: str = field(repr=False)
    url: str = REPORT_UPLOAD_URL


def load_settings(path=None):
    """Only the injected build file enables Send; never consult user config."""
    try:
        data = json.loads((Path(path) if path is not None else BUILD_CONFIG).read_text(encoding="utf-8"))
        token = data.get("token") if isinstance(data, dict) else None
        if isinstance(token, str) and re.fullmatch(r"[!-~]{1,512}", token):
            # An injected URL cannot redirect credentials to another host.
            return UploadSettings(token)
    except (OSError, ValueError, TypeError):
        pass
    return None


def safe_summary(config):
    """Constrain free-form config values before the existing summary helper."""
    from samsara.config_schema import SETTINGS_SCHEMA
    cfg = {}
    for key in ("model_size", "device", "compute_type", "performance_mode", "mode"):
        spec = SETTINGS_SCHEMA[key]
        value = config.get(key)
        if isinstance(value, str) and value in spec.get("options", ()):
            cfg[key] = value
    language = config.get("language", "en")
    cfg["language"] = language if isinstance(language, str) and re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", language) else "unavailable"
    for key in ("wake_word_enabled", "launch_at_login", "start_hidden"):
        cfg[key] = config.get(key) is True
    command = config.get("command_mode")
    cfg["command_mode"] = {"enabled": isinstance(command, dict) and command.get("enabled") is True}
    scale = config.get("ui_scale", 1.0)
    cfg["ui_scale"] = scale if type(scale) in (int, float) and 0.5 <= scale <= 4 else 1.0
    windows = ".".join(map(str, sys.getwindowsversion()[:3])) if sys.platform == "win32" else "unavailable"
    return build_safe_diagnostic_summary(cfg, platform_text=windows,
                                         python_version=".".join(map(str, sys.version_info[:3])))


@dataclass(frozen=True)
class ReviewedBundle:
    data: bytes = field(repr=False)
    digest: str
    path: Path

    def entries(self):
        with ZipFile(BytesIO(self.data)) as archive:
            return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}

    def fields(self):
        fields = json.loads(self.entries()["metadata.json"])
        return {**fields, "bundle_sha256": self.digest}

    def preview(self):
        # Decode the finished ZIP, rather than regenerating what we think it contains.
        files = self.entries()
        # Start with the useful diagnostics; all archive entries remain visible below.
        order = ["report.txt", *[name for name in files if name != "report.txt"]]
        sections = [f"ZIP entry: {name}\n{files[name]}" for name in order]
        fields = json.dumps(self.fields(), ensure_ascii=False, indent=2)
        return "\n\n".join([*sections, "Upload fields:\n" + fields,
                              "Destination: " + REPORT_UPLOAD_URL])

    def discard(self):
        self.path.unlink(missing_ok=True)


def _zip(files):
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED, allowZip64=False) as archive:
        for name, content in files.items():
            archive.writestr(ZipInfo(name, (1980, 1, 1, 0, 0, 0)), content.encode("utf-8"))
    return output.getvalue()


def build_bundle(report, *, config, directory, activity="", contact="", report_kind="problem"):
    """report must be capture.snapshot() output, never a raw log or profile dump."""
    from samsara import __version__
    from .redact import activity_text
    version = __version__ if re.fullmatch(r"[0-9A-Za-z._+-]{1,32}", __version__) else "unavailable"
    if report_kind not in ("problem", "crash", "feedback"):
        raise ValueError("Unsupported report kind")
    contact = str(contact).replace("\r", " ").replace("\n", " ")[:200]
    fields = {"app_version": version, "report_kind": report_kind,
              "summary": "Samsara " + report_kind + " report", "contact": contact}
    files = {"report.txt": report, "summary.txt": safe_summary(config),
             "activity.txt": activity_text(activity),
             "metadata.json": json.dumps(fields, ensure_ascii=False, indent=2)}
    # Only diagnostic log lines are eligible for truncation. Metadata is never silently cut.
    from .capture import DIAGNOSTICS_HEADING
    marker = DIAGNOSTICS_HEADING + "\n"
    prefix, separator, tail = report.partition(marker)
    logs, fault_marker, fault = tail.partition("\n\nPrevious-session fault")
    lines = logs.splitlines() if separator else []
    dropped = 0
    while True:
        names = ["manifest.json", *files]
        manifest = json.dumps({"files": names}, indent=2)
        data = _zip({"manifest.json": manifest, **files})
        if len(data) <= MAX_BUNDLE_BYTES:
            break
        if not lines:
            raise ValueError("Report metadata exceeds the bundle size limit")
        # Drop oldest first, enough to cover the excess, then measure the actual ZIP again.
        excess = len(data) - MAX_BUNDLE_BYTES + 128
        removed = 0
        count = 0
        for line in lines:
            removed += len(line.encode("utf-8")) + 1
            count += 1
            if removed >= excess:
                break
        lines = lines[count:]
        dropped += count
        files["report.txt"] = prefix + marker + "\n".join(lines) + fault_marker + fault
        files["truncation.txt"] = f"Size limit: dropped {dropped} oldest diagnostic log lines.\n"
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="review-", suffix=".zip", dir=directory, delete=False) as stream:
        stream.write(data)
        path = Path(stream.name)
    return ReviewedBundle(data, sha256(data).hexdigest(), path)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class UploadResult:
    report_id: str = ""
    saved_path: Path | None = None
    reason: str = ""


def _multipart(bundle):
    boundary = "SamsaraReport" + bundle.digest
    parts = []
    for name, value in bundle.fields().items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="report.zip"\r\nContent-Type: application/zip\r\n\r\n'.encode(),
                  bundle.data, f"\r\n--{boundary}--\r\n".encode()])
    return boundary, b"".join(parts)


def send_bundle(bundle, settings, fallback_directory, *, timeout=UPLOAD_TIMEOUT):
    """One HTTP attempt. Any failure preserves the reviewed bytes; never echo errors/tokens."""
    if sha256(bundle.data).hexdigest() != bundle.digest:
        raise ValueError("Reviewed bundle hash mismatch")
    boundary, body = _multipart(bundle)
    request = Request(settings.url, data=body, method="POST", headers={
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "X-Samsara-Report-Token": settings.token,
    })
    reason = "Connection failed"
    try:
        # Do not forward the token through redirects or environment-configured proxies.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(4097)
            result = json.loads(raw) if len(raw) <= 4096 else None
            report_id = result.get("id") if isinstance(result, dict) else None
            if response.status == 200 and isinstance(report_id, str) and re.fullmatch(r"SR-[A-Za-z0-9]{6,32}", report_id):
                return UploadResult(report_id=report_id)
            reason = "Invalid server response"
    except HTTPError as error:
        reason = f"Server returned {error.code}"
        error.close()
    except (TimeoutError, URLError, OSError, ValueError):
        pass
    destination = Path(fallback_directory) / ("report-" + bundle.digest + ".zip")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(bundle.data)
        return UploadResult(saved_path=destination.resolve(), reason=reason)
    except OSError:
        # The reviewed temp ZIP remains available even if the fallback directory fills up.
        return UploadResult(saved_path=bundle.path.resolve() if bundle.path.is_file() else None,
                            reason=reason + "; could not save a second copy")
