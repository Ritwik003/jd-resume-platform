"""Compile LaTeX to PDF, using whichever engine the machine happens to have.

Engine choice is deliberately not configuration. A template that needs XeLaTeX
for its fonts will not build under pdfLaTeX, and the operator should not have to
know that — so we try the most capable first and fall back. Tectonic leads
because it resolves missing packages by itself, which is the usual reason a
borrowed resume template fails to build on a fresh machine.

Nothing here raises when no engine exists: ``available()`` reports it and the
caller degrades to producing the .tex alone.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import LATEX_ENGINE_PATH, LATEX_TOOLS_DIR

logger = logging.getLogger(__name__)

#: Ordered by how much they can build without help, not by popularity.
ENGINES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tectonic", ("--keep-logs", "--print")),
    ("xelatex", ("-interaction=nonstopmode", "-halt-on-error")),
    ("lualatex", ("-interaction=nonstopmode", "-halt-on-error")),
    ("pdflatex", ("-interaction=nonstopmode", "-halt-on-error")),
)

COMPILE_TIMEOUT_SECONDS = 180

_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape(text: str | None) -> str:
    """Make arbitrary text safe to drop into a LaTeX document.

    Backslash must be handled in the same pass as the rest, otherwise escaping
    ``&`` to ``\\&`` and then escaping backslashes turns it into ``\\textbackslash{}&``.
    """
    if not text:
        return ""
    return "".join(_LATEX_SPECIALS.get(ch, ch) for ch in str(text))


@dataclass(frozen=True, slots=True)
class Engine:
    name: str
    path: str
    args: tuple[str, ...]


def _lookup(name: str) -> str | None:
    """Find ``name`` on PATH, or as a loose binary in the tools directory.

    Tectonic ships as a bare .exe rather than an installer, so requiring it on
    PATH would mean every operator edits their environment before the platform
    can make a PDF. Dropping the file in ./tools is enough.
    """
    found = shutil.which(name)
    if found:
        return found
    tools = Path(LATEX_TOOLS_DIR)
    for candidate in (tools / name, tools / f"{name}.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


@lru_cache(maxsize=1)
def find_engine() -> Engine | None:
    # An explicit path wins: it is how an operator pins a specific install.
    if LATEX_ENGINE_PATH:
        configured = Path(LATEX_ENGINE_PATH)
        if configured.is_file():
            stem = configured.stem.lower()
            args = next((a for n, a in ENGINES if n == stem), ENGINES[0][1])
            logger.info("LaTeX engine from LATEX_ENGINE_PATH: %s", configured)
            return Engine(name=stem, path=str(configured), args=args)
        logger.warning("LATEX_ENGINE_PATH is set but %s is not a file", configured)

    for name, args in ENGINES:
        path = _lookup(name)
        if path:
            logger.info("LaTeX engine: %s (%s)", name, path)
            return Engine(name=name, path=path, args=args)
    return None


def available() -> bool:
    return find_engine() is not None


def engine_name() -> str | None:
    engine = find_engine()
    return engine.name if engine else None


class LatexCompileError(RuntimeError):
    """Compilation failed. ``log`` holds the part of the output worth reading."""

    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log


#: LaTeX logs are enormous and mostly font warnings; these lines are the ones
#: that actually say what went wrong.
_ERROR_LINE = re.compile(r"^(!.*|.*?:\d+:.*|l\.\d+.*)$", re.M)


def _useful_log(text: str, limit: int = 2500) -> str:
    hits = _ERROR_LINE.findall(text or "")
    return ("\n".join(hits) if hits else (text or ""))[:limit]


# ───────────────────────────────────────────────── engine compatibility
#
# A design written on Overleaf was written against a particular engine, and
# the one installed here is rarely the same one. Two mismatches account for
# nearly every "it compiled there and not here", and both are fixable without
# touching the design's appearance.

#: pdfTeX primitives that simply do not exist in XeTeX or LuaTeX. They improve
#: how text COPIES OUT of the finished PDF and have no effect on the page, so
#: dropping them under a non-pdfTeX engine costs the design nothing and is the
#: difference between a PDF and an error.
_PDFTEX_ONLY = (
    # Both spellings. TeX's \input takes a space-delimited filename as well as
    # a braced one, and the popular developer templates use the bare form —
    # "\input glyphtounicode" — so matching only the braced one let the file
    # through and the compile died inside it.
    re.compile(r"^[ \t]*\\input\s*\{\s*glyphtounicode\s*\}.*$", re.MULTILINE),
    re.compile(r"^[ \t]*\\input\s+glyphtounicode\b.*$", re.MULTILINE),
    # ".*$" rather than "\s*$": these lines carry a trailing comment as often
    # as not ("\pdfgentounicode=1 % ensures pdfs are ats readable"), and
    # anchoring to end-of-line without allowing one missed them all.
    re.compile(r"^[ \t]*\\pdfgentounicode\s*=\s*\d+.*$", re.MULTILINE),
    re.compile(r"^[ \t]*\\pdfglyphtounicode\b.*$", re.MULTILINE),
    re.compile(r"^[ \t]*\\pdfmapfile\b.*$", re.MULTILINE),
)

#: fontspec asking for a font BY NAME needs that font installed on the machine.
#: When it is not, the compile dies in the preamble and nothing renders. The
#: retry below neutralises the request so the design prints in the engine's
#: default face — a different typeface, but a readable resume instead of none.
_FONT_SELECTION = re.compile(
    r"^[ \t]*\\(?:setmainfont|setsansfont|setmonofont|setromanfont|newfontfamily"
    r"|setmathfont|defaultfontfeatures)\b[^\n]*$",
    re.MULTILINE,
)
_MISSING_FONT = re.compile(r"font\s+\"?[^\"\n]+\"?\s+cannot be found", re.IGNORECASE)


def _for_engine(tex_source: str, engine_name: str) -> str:
    """The source adjusted for the engine that is actually installed."""
    if engine_name in ("pdflatex", "latexmk"):
        return tex_source
    cleaned = tex_source
    for pattern in _PDFTEX_ONLY:
        cleaned = pattern.sub("% (removed: pdfTeX-only, unsupported by this engine)", cleaned)
    return cleaned


def _without_font_selection(tex_source: str) -> str:
    """The source with its font choices dropped, for the missing-font retry."""
    return _FONT_SELECTION.sub("% (removed: font not installed on this machine)", tex_source)


def compile_pdf(
    tex_source: str,
    output_path: str | Path,
    *,
    passes: int = 2,
    assets_dir: str | Path | None = None,
) -> Path:
    """Compile ``tex_source`` and put the PDF at ``output_path``.

    Runs in a scratch directory so the template's auxiliary files never litter
    the resume store, and twice by default so cross-references and page totals
    settle — a "Page 1 of ??" resume is a common one-pass artefact. Tectonic
    resolves references itself and only needs one pass.

    ``assets_dir`` is copied into the scratch directory first. An Overleaf
    project is rarely one file — a .cls beside the .tex, a .sty, a logo — and
    LaTeX resolves those relative to the file being compiled, so they have to
    travel with it. The main .tex from that directory is NOT used: the source
    passed in is the filled-in one, and it replaces its own original.
    """
    engine = find_engine()
    if engine is None:
        raise LatexCompileError(
            "No LaTeX engine found. Put tectonic.exe in ./tools, set "
            "LATEX_ENGINE_PATH, or install MiKTeX (winget install MiKTeX.MiKTeX)."
        )

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="jdresume-tex-") as tmp:
        work = Path(tmp)

        if assets_dir is not None:
            assets = Path(assets_dir)
            if assets.is_dir():
                for item in assets.iterdir():
                    # Skip .tex files: the source being compiled is the filled-in
                    # version of the project's own main file, and a stale copy
                    # sitting beside it only invites compiling the wrong one.
                    if item.is_file() and item.suffix.lower() != ".tex":
                        shutil.copyfile(item, work / item.name)
                    elif item.is_dir():
                        shutil.copytree(item, work / item.name, dirs_exist_ok=True)

        tex_file = work / "resume.tex"
        prepared = _for_engine(tex_source, engine.name)
        tex_file.write_text(prepared, encoding="utf-8")

        runs = 1 if engine.name == "tectonic" else max(1, passes)
        last = None
        attempted_without_fonts = False
        for attempt in range(runs):
            cmd = [engine.path, *engine.args]
            if engine.name != "tectonic":
                cmd += [f"-output-directory={work}"]
            cmd.append(str(tex_file))
            try:
                last = subprocess.run(  # noqa: S603 — fixed argv, no shell
                    cmd,
                    cwd=work,
                    capture_output=True,
                    text=True,
                    timeout=COMPILE_TIMEOUT_SECONDS,
                    # Keep a stray \read or \write18 from stalling on stdin.
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, "TEXMFVAR": str(work / "texmf")},
                )
            except subprocess.TimeoutExpired as exc:
                raise LatexCompileError(
                    f"{engine.name} did not finish within {COMPILE_TIMEOUT_SECONDS}s. "
                    "The template may be waiting on input or stuck in a loop."
                ) from exc
            if attempt == runs - 1 or last.returncode != 0:
                break

        produced = work / "resume.pdf"

        if not produced.exists() and not attempted_without_fonts:
            combined = (last.stdout if last else "") + "\n" + (last.stderr if last else "")
            if _MISSING_FONT.search(combined):
                # The design asked for a typeface this machine does not have.
                # Printing it in the default face is a compromise; printing
                # nothing is not a resume.
                attempted_without_fonts = True
                logger.warning("A font this design asks for is not installed; retrying without it")
                tex_file.write_text(_without_font_selection(prepared), encoding="utf-8")
                # The font choice usually lives in the project's .cls or .sty
                # rather than in the document, so those have to be rewritten
                # too or the retry hits exactly the same line.
                for support in list(work.glob("*.cls")) + list(work.glob("*.sty")):
                    try:
                        support.write_text(
                            _without_font_selection(support.read_text(encoding="utf-8", errors="replace")),
                            encoding="utf-8",
                        )
                    except OSError:
                        continue
                for attempt in range(runs):
                    cmd = [engine.path, *engine.args]
                    if engine.name != "tectonic":
                        cmd += [f"-output-directory={work}"]
                    cmd.append(str(tex_file))
                    try:
                        last = subprocess.run(  # noqa: S603 — fixed argv, no shell
                            cmd,
                            cwd=work,
                            capture_output=True,
                            text=True,
                            timeout=COMPILE_TIMEOUT_SECONDS,
                            stdin=subprocess.DEVNULL,
                            env={**os.environ, "TEXMFVAR": str(work / "texmf")},
                        )
                    except subprocess.TimeoutExpired:
                        break
                    if attempt == runs - 1 or last.returncode != 0:
                        break
                produced = work / "resume.pdf"

        if not produced.exists():
            log = _useful_log((last.stdout if last else "") + "\n" + (last.stderr if last else ""))
            # Put the first real error in the message itself. "See the log" sends
            # the reader looking for a log they cannot reach from the UI, and the
            # one line they need is right here.
            headline = next(
                (line.strip() for line in log.splitlines() if line.strip().startswith("!")),
                "",
            )
            detail = f" {headline}" if headline else ""
            raise LatexCompileError(
                f"{engine.name} produced no PDF.{detail}", log=log
            )

        shutil.copyfile(produced, output)
    return output


def compile_to_bytes(
    tex_source: str, *, passes: int = 2, assets_dir: str | Path | None = None
) -> bytes:
    """The compiled PDF itself, for callers with nowhere to put a file.

    Used by the upload-time verification pass, which only needs to know
    whether a design compiles — not to keep what it produced.
    """
    with tempfile.TemporaryDirectory(prefix="jdresume-out-") as tmp:
        out = Path(tmp) / "out.pdf"
        compile_pdf(tex_source, out, passes=passes, assets_dir=assets_dir)
        return out.read_bytes()
