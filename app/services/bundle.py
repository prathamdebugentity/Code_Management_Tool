from __future__ import annotations

import shutil
import zipfile
from pathlib import Path, PurePosixPath

_ENTRYPOINTS = {"Procfile", "application.py", "Dockerfile", "docker-compose.yml", "package.json", "requirements.txt"}


class BundleError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _is_unsafe(name: str) -> bool:
    return name.startswith("/") or "\\" in name or ".." in PurePosixPath(name).parts


def inspect_bundle(path: Path, max_uncompressed: int) -> list[str]:
    """Validate a deployable zip and return non-fatal warnings."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise BundleError("File is not a valid zip archive.") from exc
    with zf:
        files = [i for i in zf.infolist() if not i.is_dir()]
        if not files:
            raise BundleError("Zip archive is empty.")
        if sum(i.file_size for i in files) > max_uncompressed:
            raise BundleError("Bundle exceeds the maximum uncompressed size.", 413)
        if any(_is_unsafe(i.filename) for i in files):
            raise BundleError("Bundle contains unsafe file paths.")
        names = [i.filename for i in files]

    warnings: list[str] = []
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) == 1 and all("/" in n for n in names):
        warnings.append(
            f"All files are inside a single folder '{next(iter(tops))}/'. "
            "Elastic Beanstalk expects application files at the zip root."
        )
    if not _ENTRYPOINTS.intersection(names):
        warnings.append("No Procfile, application.py, Dockerfile or package.json at the bundle root.")
    return warnings


def repack_github_archive(src: Path, dest: Path, max_uncompressed: int) -> int:
    """GitHub zipballs wrap everything in '<owner>-<repo>-<sha>/'; strip that so files sit at the bundle root."""
    total = 0
    count = 0
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.is_dir():
                    continue
                _, sep, rel = info.filename.partition("/")
                if not sep or not rel:
                    continue
                if _is_unsafe(rel):
                    raise BundleError(f"Repository archive contains an unsafe path: {rel}")
                total += info.file_size
                if total > max_uncompressed:
                    raise BundleError("Repository exceeds the maximum uncompressed bundle size.", 413)
                out = zipfile.ZipInfo(rel, date_time=info.date_time)
                out.external_attr = info.external_attr
                out.compress_type = zipfile.ZIP_DEFLATED
                with zin.open(info) as s, zout.open(out, "w") as d:
                    shutil.copyfileobj(s, d, 1024 * 1024)
                count += 1
    except zipfile.BadZipFile as exc:
        raise BundleError("GitHub returned an invalid archive.") from exc
    if count == 0:
        raise BundleError("Repository archive contains no files.")
    return count
