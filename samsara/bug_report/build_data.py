"""PyInstaller data hook; contains no credentials or application startup."""
import json
from pathlib import Path


def report_datas(root, directory, *, upload_config=None):
    from .code_index import create_index
    from .metadata import build_commit
    root, directory = Path(root), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    metadata = directory / "build_info.json"
    metadata.write_text(json.dumps({"commit": build_commit(root, frozen=False)}), encoding="utf-8")
    code = directory / "report_code.json"
    code.write_text(json.dumps(create_index(root)), encoding="utf-8")
    result = [(str(metadata), "release"), (str(code), "samsara/data")]
    # The release runner supplies the existing receiver credential by path;
    # no secret is read, printed or written to the public source tree here.
    injected = Path(upload_config) if upload_config else root / "samsara/data/report_upload.json"
    if injected.is_file():
        result.append((str(injected), "samsara/data"))
    return result
