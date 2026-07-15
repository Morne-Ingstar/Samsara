from pathlib import Path


REQUIREMENTS = (
    Path(__file__).resolve().parents[1] / "requirements.txt"
).read_text(encoding="utf-8").splitlines()


def _declared_packages() -> dict[str, str]:
    result = {}
    for raw in REQUIREMENTS:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = line.split(";", 1)[0]
        for marker in ("==", ">=", "<=", "~=", ">", "<"):
            name = name.split(marker, 1)[0]
        result[name.strip().casefold()] = line
    return result


def test_mediapipe_uses_one_compatible_opencv_distribution():
    packages = _declared_packages()
    assert "opencv-contrib-python" in packages
    assert "opencv-python" not in packages


def test_mediapipe_protobuf_contract_is_pinned_below_four():
    protobuf = _declared_packages()["protobuf"].replace(" ", "")
    assert ">=3.20.3" in protobuf
    assert "<4" in protobuf
