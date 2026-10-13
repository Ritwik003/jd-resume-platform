r"""An uploaded template is a PROJECT, not a file.

Overleaf hands you a folder: a main ``.tex``, usually a ``.cls`` or a couple of
``.sty`` files that carry the actual design, sometimes an image, sometimes the
sections split across ``\input`` files. Uploading only the ``.tex`` from such a
project gives you a document that cannot compile and a design nobody can see,
which is why this module takes the whole thing.

Three jobs, in order:

1. **Unpack** — files, or a ``.zip`` of them, into one directory per template.
   That directory is what later compiles: ``latex.compile_pdf`` copies it into
   the scratch dir so a ``.cls`` sits beside the ``.tex`` exactly as it did on
   Overleaf.
2. **Find the main file** — the one carrying ``\documentclass``. A project has
   exactly one, and guessing wrong means templatizing a fragment.
3. **Flatten** — resolve ``\input``/``\include`` of other ``.tex`` files into
   one source. Section headings often live in those, and a blueprint that
   cannot see them would offer the operator half the design. Class and style
   files are deliberately NOT flattened: they stay on disk and are compiled
   beside the document, where LaTeX expects them.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import zipfile
from pathlib import Path

from app.utils.exceptions import ValidationError

logger = logging.getLogger(__name__)

#: What an Overleaf project legitimately contains. Anything else in the zip is
#: dropped rather than written to disk — an upload is not an opportunity to
#: place arbitrary files on the server.
ALLOWED_SUFFIXES = {
    ".tex", ".cls", ".sty", ".bib", ".bst", ".def", ".clo", ".ldf", ".cfg",
    ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg", ".ttf", ".otf", ".fd",
}
#: Junk every zip from a real machine carries.
_IGNORED_PARTS = ("__MACOSX", ".git", ".DS_Store")

MAX_PROJECT_FILES = 200
MAX_PROJECT_BYTES = 40 * 1024 * 1024

_DOCUMENTCLASS = re.compile(r"^\s*\\documentclass", re.MULTILINE)
_INPUT = re.compile(r"\\(?:input|include)\s*\{\s*([^{}]+?)\s*\}")
_COMMENTED = re.compile(r"(?<!\\)%[^\n]*")


def _safe_name(name: str) -> str | None:
    """The path to store an uploaded member at, or ``None`` to drop it.

    Zip members carry whatever path the creator's machine had, including
    ``../`` and absolute roots. Only a relative path made of ordinary
    components survives here.
    """
    raw = (name or "").replace("\\", "/").strip()
    if not raw or raw.endswith("/"):
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    if any(part in _IGNORED_PARTS for part in parts) or parts[-1].startswith("."):
        return None
    if Path(parts[-1]).suffix.lower() not in ALLOWED_SUFFIXES:
        return None
    return "/".join(parts)


def _write(root: Path, relative: str, data: bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def unpack(files: list[tuple[str, bytes]], destination: Path) -> list[Path]:
    """Write an upload into ``destination``, expanding any zip it contains.

    ``files`` is ``(filename, bytes)`` — the shape a FastAPI ``UploadFile``
    reduces to, kept deliberately plain so this is testable without HTTP.
    """
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    total = 0

    for name, data in files:
        if Path(name or "").suffix.lower() == ".zip":
            try:
                archive = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile as exc:
                raise ValidationError(f"'{name}' is not a readable .zip file.") from exc
            with archive:
                members = [m for m in archive.infolist() if not m.is_dir()]
                kept = [(m, rel) for m, rel in ((m, _safe_name(m.filename)) for m in members) if rel]
                # Strip a single wrapping folder — a zip made from Overleaf is
                # usually one directory deep, and keeping it would put the .cls
                # in a subfolder where the compile cannot see it.
                roots = {rel.split("/")[0] for _, rel in kept if "/" in rel}
                strip = len(roots) == 1 and all("/" in rel for _, rel in kept)
                for member, relative in kept:
                    if len(written) >= MAX_PROJECT_FILES:
                        break
                    payload = archive.read(member)
                    total += len(payload)
                    if total > MAX_PROJECT_BYTES:
                        raise ValidationError(
                            f"That project unpacks to more than {MAX_PROJECT_BYTES // 1024 // 1024} MB."
                        )
                    if strip:
                        relative = relative.split("/", 1)[1]
                    written.append(_write(destination, relative, payload))
            continue

        relative = _safe_name(name)
        if not relative:
            logger.info("Ignoring uploaded file %r: not a template file type", name)
            continue
        total += len(data)
        if total > MAX_PROJECT_BYTES:
            raise ValidationError(
                f"That project is larger than {MAX_PROJECT_BYTES // 1024 // 1024} MB."
            )
        written.append(_write(destination, relative, data))

    if not written:
        raise ValidationError(
            "No usable template files in that upload. A template project is a .tex "
            "file, optionally with the .cls/.sty/image files it needs — or a .zip of them."
        )
    return written


#: The shape a browser gives a second copy of a file it has already saved:
#: "main (1).tex". Several of those in one folder means several templates were
#: downloaded into the same place, not that one template has spare parts.
_DOWNLOAD_COPY = re.compile(r"\s\(\d+\)(?=\.[A-Za-z]+$)")


def documents(root: Path) -> list[Path]:
    r"""Every file in the project that carries ``\documentclass``.

    A project normally has exactly one. More than one means either a design
    that ships an example alongside its template, or — far more often — that
    somebody downloaded several templates into a single folder.
    """
    found: list[Path] = []
    for path in sorted(root.rglob("*.tex")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _DOCUMENTCLASS.search(_COMMENTED.sub("", text)):
            found.append(path)
    return found


def assert_single_project(root: Path) -> None:
    r"""Refuse a folder that is really several templates stacked together.

    This is worth its own check because of how it fails otherwise. Each
    ``main (N).tex`` says ``\documentclass{resume_config}``, and there is only
    one ``resume_config.cls`` — so LaTeX silently pairs a document with
    another template's class file and dies on an undefined macro thirty lines
    in. The operator sees a compiler error about a command they have never
    heard of, when the real problem is that three templates are in one folder.
    """
    docs = documents(root)
    copies = [d for d in docs if _DOWNLOAD_COPY.search(d.name)]
    if len(docs) > 1 and copies:
        names = ", ".join(sorted(d.name for d in docs))
        raise ValidationError(
            f"That folder holds {len(docs)} separate templates ({names}), not one "
            "template's files. Names like 'main (1).tex' are what a browser calls "
            "a second download of the same file, so the .cls each document needs "
            "is no longer the one it asks for. Upload one template per folder, "
            "with its own main .tex and the .cls named exactly as that .tex "
            "requires."
        )


def find_main_tex(root: Path) -> Path:
    r"""The file carrying ``\documentclass``.

    Ties are broken towards the shallowest path and then the conventional
    names, because a project with two compilable files (``main.tex`` and an
    ``example.tex``) means the shorter path and the obvious name.
    """
    candidates = documents(root)
    if not candidates:
        raise ValidationError(
            "None of the uploaded .tex files has a \\documentclass line, so there is "
            "no main document to compile. Include the project's main .tex file."
        )

    preferred = ("main.tex", "resume.tex", "cv.tex", "template.tex")

    def rank(path: Path) -> tuple[int, int, str]:
        name = path.name.lower()
        return (
            len(path.relative_to(root).parts),
            preferred.index(name) if name in preferred else len(preferred),
            name,
        )

    return sorted(candidates, key=rank)[0]


def flatten(main: Path, root: Path | None = None, *, _depth: int = 0) -> str:
    r"""``main``'s source with its ``\input``/``\include`` of .tex files inlined.

    Only files that exist inside the project are inlined; ``\input`` of a
    system file is left alone for LaTeX to resolve at compile time. Recursion
    is capped because a project that inputs itself would otherwise not stop.
    """
    base = root or main.parent
    text = main.read_text(encoding="utf-8", errors="replace")
    if _depth >= 5:
        return text

    def resolve(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        if Path(raw).suffix.lower() in (".sty", ".cls"):
            return match.group(0)
        for candidate in (base / raw, base / f"{raw}.tex", main.parent / raw, main.parent / f"{raw}.tex"):
            try:
                inside = str(candidate.resolve()).startswith(str(base.resolve()))
            except (OSError, ValueError):
                continue
            if inside and candidate.is_file() and candidate.suffix.lower() == ".tex":
                return f"% -- inlined from {raw}\n" + flatten(candidate, base, _depth=_depth + 1)
        return match.group(0)

    return _INPUT.sub(resolve, text)
