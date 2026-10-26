"""Fills the knowledge folder from public sources, so the assistant can learn from other labs.

  python -m protocol_tool.knowledge_import opentrons-library
      Every protocol in the Opentrons Protocol Library (github.com/Opentrons/Protocols):
      description, default parameters and code -> knowledge/protocols/opentrons-library/
  python -m protocol_tool.knowledge_import opentrons-docs
      The Opentrons Python API documentation (Flex and OT-2, Apache-2.0)
      -> knowledge/reference/opentrons-api/
  python -m protocol_tool.knowledge_import protocols-io "ELISA" "qPCR" --limit 25
      Public protocols on protocols.io matching each search (CC BY 4.0; needs a free token,
      PROTOCOLS_IO_TOKEN in .env) -> knowledge/protocols/protocols-io/
  python -m protocol_tool.knowledge_import github owner/repo [owner/repo ...]
      Every Opentrons protocol in another lab's public GitHub repository, with the repository's
      license -> knowledge/protocols/github/
  python -m protocol_tool.knowledge_import url https://example.com/kit-manual.pdf [--folder manuals]
      Any document on the web (kit manual, IFU, paper; PDF, HTML or text).

Each imported document records its source, authors and terms (in the document and in
knowledge/sources.json), so the assistant can cite it. Running an import again
refreshes it. Downloads are only read as text, never run.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import logging
import os
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .config import load_settings
from .knowledge import SOURCES_FILE
from .model_status import load_key_file

log = logging.getLogger(__name__)

OPENTRONS_LIBRARY_ZIP = "https://codeload.github.com/Opentrons/Protocols/zip/refs/heads/develop"
OPENTRONS_TREE = "https://api.github.com/repos/Opentrons/opentrons/git/trees/edge?recursive=1"
OPENTRONS_RAW = "https://raw.githubusercontent.com/Opentrons/opentrons/edge/"
# The Python Protocol API docs (Markdown, MkDocs) and the official example protocols.
OPENTRONS_DOCS_DIR = "docs/python-api/docs/"
OPENTRONS_EXAMPLES_DIR = "api/docs/v2/example_protocols/"
PROTOCOLS_IO = "https://www.protocols.io"
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
MAX_CODE_CHARS = 40_000
USER_AGENT = "lab-protocol-tool knowledge import"

LIBRARY_TERMS = (
    "From the Opentrons Protocol Library (Opentrons and community authors). Kept as an internal "
    "reference for this lab; credit the authors and don't republish."
)
DOCS_LICENSE = "Apache-2.0 (Opentrons)"
PROTOCOLS_IO_LICENSE = "CC BY 4.0 (protocols.io public content): cite the authors and DOI"


class ImportProblem(Exception):
    """An import can't go ahead (missing token, bad URL, download failed)."""


def slugify(text: str, limit: int = 80) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "document"


class Sources:
    """knowledge/sources.json: where each imported document came from."""

    def __init__(self, root: Path):
        self.path = root / SOURCES_FILE
        try:
            self.entries: dict[str, dict] = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except ValueError:
            self.entries = {}

    def add(self, doc_id: str, **info: Any) -> None:
        self.entries[doc_id] = {**info, "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(dict(sorted(self.entries.items())), indent=1), encoding="utf-8")


def _write(root: Path, doc_id: str, text: str) -> None:
    path = root / doc_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _client(timeout: float = 60.0, transport: httpx.BaseTransport | None = None, headers: dict | None = None) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True, transport=transport,
                        headers={"User-Agent": USER_AGENT, **(headers or {})})


def _download(client: httpx.Client, url: str, dest: io.BufferedIOBase, progress: Callable[[str], None]) -> None:
    with client.stream("GET", url) as response:
        if response.is_error:
            raise ImportProblem(f"{url} returned {response.status_code}")
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise ImportProblem(f"{url} is larger than {MAX_DOWNLOAD_BYTES // 2**20} MB")
            dest.write(chunk)
            if total // (5 * 2**20) != (total - len(chunk)) // (5 * 2**20):
                progress(f"  downloaded {total // 2**20} MB")


# --- Opentrons Protocol Library --------------------------------------------------------------


def import_opentrons_library(root: Path, *, transport: httpx.BaseTransport | None = None,
                             progress: Callable[[str], None] = print) -> int:
    """Every Python API v2 protocol in the library, one Markdown document each."""
    progress("Downloading the Opentrons Protocol Library (github.com/Opentrons/Protocols)...")
    with tempfile.TemporaryFile() as archive, _client(300, transport) as client:
        _download(client, OPENTRONS_LIBRARY_ZIP, archive, progress)
        archive.seek(0)
        with zipfile.ZipFile(archive) as z:
            return _library_from_zip(root, z, progress)


def _library_from_zip(root: Path, z: zipfile.ZipFile, progress: Callable[[str], None]) -> int:
    folders: dict[str, dict[str, zipfile.ZipInfo]] = {}
    for info in z.infolist():
        parts = info.filename.split("/")
        # <repo>-<branch>/protocols/<name>/<file>
        if len(parts) == 4 and parts[1] == "protocols" and parts[3] and not info.is_dir():
            folders.setdefault(parts[2], {})[parts[3]] = info
    sources = Sources(root)
    target = root / "protocols" / "opentrons-library"
    count = 0
    for name, files in sorted(folders.items()):
        code_name = next((f for f in files if f.endswith(".ot2.apiv2.py") or f.endswith(".flex.py") or f.endswith(".apiv2.py")), None)
        if code_name is None or ".hide-from-search" in files or "README.md" not in files:
            continue  # API v1 only, hidden from the library, or undocumented
        readme = z.read(files["README.md"]).decode("utf-8", errors="replace").strip()
        code = z.read(files[code_name]).decode("utf-8", errors="replace")
        if len(code) > MAX_CODE_CHARS:
            code = code[:MAX_CODE_CHARS] + "\n# ... (cut: longer than the knowledge import keeps)"
        title = next((line.lstrip("#").strip() for line in readme.splitlines() if line.startswith("#")), name)
        url = f"https://library.opentrons.com/p/{name}"
        robot = "Flex" if ".flex." in code_name or '"robotType": "Flex"' in code else "OT-2"
        parts = [
            f"# {title}",
            "",
            f"Source: Opentrons Protocol Library, {url}",
            f"Robot: {robot}, Opentrons Python API v2"
            + (". Written for the OT-2: on the Flex use Flex pipettes, tip racks, labware and deck slots (A1-D4)." if robot == "OT-2" else "."),
            f"Terms: {LIBRARY_TERMS}",
            "",
            readme,
        ]
        if "fields.json" in files:
            fields = z.read(files["fields.json"]).decode("utf-8", errors="replace")
            if len(fields) < 6000:
                parts += ["", "## Parameters (fields.json; the code reads them with get_values)", "```json", fields.strip(), "```"]
        parts += ["", f"## Protocol code ({code_name})", "```python", code.rstrip(), "```", ""]
        doc_id = f"protocols/opentrons-library/{slugify(name)}.md"
        _write(root, doc_id, "\n".join(parts))
        sources.add(doc_id, source="Opentrons Protocol Library", url=url, license=LIBRARY_TERMS, robot=robot)
        count += 1
        if count % 200 == 0:
            progress(f"  {count} protocols written")
    sources.save()
    progress(f"Imported {count} protocols into {target}")
    return count


# --- Opentrons Python API documentation ---------------------------------------------------------


def import_opentrons_docs(root: Path, *, transport: httpx.BaseTransport | None = None,
                          progress: Callable[[str], None] = print) -> int:
    """The Python Protocol API docs and official example protocols from the Opentrons monorepo."""
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    sources = Sources(root)
    count = 0
    progress("Downloading the Opentrons Python API documentation (github.com/Opentrons/opentrons)...")
    with _client(60, transport, headers) as client:
        tree = client.get(OPENTRONS_TREE)
        if tree.is_error:
            raise ImportProblem(f"GitHub returned {tree.status_code}: {tree.text[:200]}"
                                + (" (rate limit: set GITHUB_TOKEN, or try again in an hour)" if tree.status_code == 403 else ""))
        paths = [e["path"] for e in tree.json().get("tree", []) if e.get("type") == "blob"]
        pages = [p for p in paths if p.startswith(OPENTRONS_DOCS_DIR) and p.endswith(".md")]
        examples = [p for p in paths if p.startswith(OPENTRONS_EXAMPLES_DIR) and p.endswith(".py")]
        if not pages:
            raise ImportProblem(f"No documentation found under {OPENTRONS_DOCS_DIR}; the Opentrons repository layout has changed")
        for path in pages + examples:
            response = client.get(OPENTRONS_RAW + path)
            if response.is_error:
                progress(f"  skipped {path}: {response.status_code}")
                continue
            text = response.text
            if path in examples:
                name = Path(path).stem
                url = f"https://github.com/Opentrons/opentrons/blob/edge/{path}"
                robot = "Flex" if "flex" in name else "OT-2"
                doc_id = f"reference/opentrons-api/example-{slugify(name)}.md"
                body = [f"# Official example protocol: {name} ({robot})", "", f"Source: {url}", f"License: {DOCS_LICENSE}", "",
                        "```python", text.rstrip(), "```", ""]
            else:
                rel = path.removeprefix(OPENTRONS_DOCS_DIR).removesuffix(".md")
                url = "https://docs.opentrons.com/python-api/" + (rel.removesuffix("index").rstrip("/") + "/" if rel != "index" else "")
                title = _md_title(text) or rel
                doc_id = f"reference/opentrons-api/{slugify(rel)}.md"
                body = [f"# {title}", "", f"Source: Opentrons Python Protocol API documentation, {url}",
                        f"License: {DOCS_LICENSE}", "", _without_front_matter(text).strip(), ""]
            _write(root, doc_id, "\n".join(body))
            sources.add(doc_id, source="Opentrons Python API docs", url=url, license=DOCS_LICENSE)
            count += 1
    sources.save()
    progress(f"Imported {count} documentation pages and examples into {root / 'reference' / 'opentrons-api'}")
    return count


def _without_front_matter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def _md_title(text: str) -> str:
    match = re.search(r"^title:\s*['\"]?(.+?)['\"]?\s*$", text[:2000], re.MULTILINE) if text.startswith("---") else None
    if match:
        return match.group(1)
    for line in _without_front_matter(text).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# --- Any lab's GitHub repository of Opentrons protocols -----------------------------------------------


def import_github(root: Path, repo: str, *, transport: httpx.BaseTransport | None = None,
                  progress: Callable[[str], None] = print) -> int:
    """Every Opentrons protocol (.py with a run() function) in a public GitHub repository, with its license."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ImportProblem("Give the repository as owner/name, e.g. openplant/openplant_automation_protocols")
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    sources = Sources(root)
    count = 0
    with _client(60, transport, headers) as client:
        info = client.get(f"https://api.github.com/repos/{repo}")
        if info.is_error:
            raise ImportProblem(f"GitHub returned {info.status_code} for {repo}")
        meta = info.json()
        branch = meta.get("default_branch", "main")
        spdx = ((meta.get("license") or {}).get("spdx_id") or "").replace("NOASSERTION", "")
        license = spdx or "no license stated: internal reference only, credit the authors, don't republish"
        tree = client.get(f"https://api.github.com/repos/{repo}/git/trees/{branch}", params={"recursive": "1"})
        if tree.is_error:
            raise ImportProblem(f"GitHub returned {tree.status_code} listing {repo}")
        paths = [e["path"] for e in tree.json().get("tree", []) if e.get("type") == "blob" and e["path"].endswith(".py")]
        progress(f"{repo} ({license}): checking {len(paths)} Python files...")
        for path in paths:
            response = client.get(f"https://raw.githubusercontent.com/{repo}/{branch}/{path}")
            if response.is_error:
                continue
            code = response.text
            if "opentrons" not in code or not re.search(r"^def run\(", code, re.MULTILINE):
                continue  # not an Opentrons protocol
            if len(code) > MAX_CODE_CHARS:
                code = code[:MAX_CODE_CHARS] + "\n# ... (cut: longer than the knowledge import keeps)"
            name_match = re.search(r"""["']protocolName["']\s*:\s*["'](.+?)["']""", code)
            title = name_match.group(1) if name_match else Path(path).stem.replace("_", " ")
            robot = "Flex" if re.search(r"""["']robotType["']\s*:\s*["']Flex["']""", code) else "OT-2"
            url = f"https://github.com/{repo}/blob/{branch}/{path}"
            doc_id = f"protocols/github/{slugify(repo, 50)}/{slugify(path.removesuffix('.py'), 80)}.md"
            _write(root, doc_id, "\n".join([
                f"# {title}", "", f"Source: {url}", f"Robot: {robot}, Opentrons Python API"
                + (". Written for the OT-2: on the Flex use Flex pipettes, tip racks, labware and deck slots." if robot == "OT-2" else "."),
                f"License: {license}", "", "```python", code.rstrip(), "```", "",
            ]))
            sources.add(doc_id, source=f"github.com/{repo}", url=url, license=license, robot=robot)
            count += 1
    sources.save()
    progress(f"Imported {count} protocols from {repo}")
    return count


# --- protocols.io --------------------------------------------------------------------------------


def import_protocols_io(root: Path, queries: list[str], *, limit: int = 20, token: str | None = None,
                        transport: httpx.BaseTransport | None = None, progress: Callable[[str], None] = print) -> int:
    """Public protocols matching each query, with their steps, one Markdown document each."""
    load_key_file("PROTOCOLS_IO_TOKEN")
    token = token or os.environ.get("PROTOCOLS_IO_TOKEN")
    if not token:
        raise ImportProblem(
            "protocols.io needs an access token. Sign in at https://www.protocols.io/developers, copy the "
            "client access token, and add PROTOCOLS_IO_TOKEN=<token> to protocol-tool\\.env."
        )
    sources = Sources(root)
    count = 0
    with _client(60, transport, {"Authorization": f"Bearer {token}"}) as client:
        for query in queries:
            progress(f"Searching protocols.io for '{query}'...")
            found = _get(client, f"{PROTOCOLS_IO}/api/v3/protocols",
                         params={"filter": "public", "key": query, "page_size": min(limit, 100), "order_field": "relevance"})
            for item in (found.get("items") or [])[:limit]:
                try:
                    doc_id = _protocols_io_one(client, root, item, sources)
                except ImportProblem as exc:
                    progress(f"  skipped '{item.get('title')}': {exc}")
                    continue
                if doc_id:
                    count += 1
                    progress(f"  {doc_id}")
    sources.save()
    progress(f"Imported {count} protocols.io protocols into {root / 'protocols' / 'protocols-io'}")
    return count


def _get(client: httpx.Client, url: str, **kwargs: Any) -> dict:
    response = client.get(url, **kwargs)
    if response.status_code == 401:
        raise ImportProblem("protocols.io refused the token (401); check PROTOCOLS_IO_TOKEN")
    if response.is_error:
        raise ImportProblem(f"{url} returned {response.status_code}: {response.text[:200]}")
    data = response.json()
    if isinstance(data, dict) and data.get("status_code") not in (None, 0):
        raise ImportProblem(f"{url}: {data.get('error_message') or data.get('status_text') or data['status_code']}")
    return data


def _protocols_io_one(client: httpx.Client, root: Path, item: dict, sources: Sources) -> str | None:
    pid = item.get("id")
    if not pid:
        return None
    data = _get(client, f"{PROTOCOLS_IO}/api/v4/protocols/{pid}", params={"content_format": "markdown"})
    protocol = data.get("payload") or data.get("protocol") or {}
    if protocol.get("public") is False:
        return None
    steps = protocol.get("steps")
    if not steps:
        steps = _get(client, f"{PROTOCOLS_IO}/api/v4/protocols/{pid}/steps", params={"content_format": "markdown"}).get("steps") or []
    title = _plain(protocol.get("title") or item.get("title") or f"protocols.io {pid}")
    authors = ", ".join(a.get("name", "") for a in (protocol.get("authors") or item.get("authors") or []) if a.get("name"))
    doi = protocol.get("doi") or item.get("doi") or ""
    url = protocol.get("url") or (f"https://www.protocols.io/view/{protocol.get('uri') or item.get('uri')}" if (protocol.get("uri") or item.get("uri")) else "")
    lines = [
        f"# {title}", "",
        f"Source: protocols.io, {url}" + (f" (doi:{doi.removeprefix('dx.doi.org/')})" if doi else ""),
        f"Authors: {authors or 'see the source'}",
        f"License: {PROTOCOLS_IO_LICENSE}", "",
    ]
    for key, heading in (("description", "Description"), ("guidelines", "Guidelines"), ("before_start", "Before start"),
                         ("warning", "Warnings"), ("materials_text", "Materials")):
        text = _plain(protocol.get(key))
        if text:
            lines += [f"## {heading}", text, ""]
    lines.append("## Steps")
    section = None
    for n, step in enumerate(steps, start=1):
        sec = _plain(step.get("section"))
        if sec and sec != section:
            lines += ["", f"### {sec}"]
            section = sec
        text = _plain(step.get("step")) or _components_text(step.get("components"))
        lines.append(f"{n}. {text}")
    doc_id = f"protocols/protocols-io/{pid}-{slugify(title, 60)}.md"
    _write(root, doc_id, "\n".join(lines) + "\n")
    sources.add(doc_id, source="protocols.io", url=url, doi=doi, authors=authors, license=PROTOCOLS_IO_LICENSE)
    return doc_id


def _components_text(components: Any) -> str:
    """Step text from the older component layout: the text fields of each component."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in ("body", "description", "text", "title") and isinstance(inner, str) and inner.strip():
                    found.append(_plain(inner))
                else:
                    walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    walk(components)
    return " ".join(t for t in found if t)


def _plain(value: Any) -> str:
    """Text from protocols.io fields: Markdown, HTML, or Draft.js JSON."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    text = str(value).strip()
    if text.startswith("{") and '"blocks"' in text:
        try:
            return "\n".join(b.get("text", "") for b in json.loads(text).get("blocks", [])).strip()
        except ValueError:
            pass
    if "<" in text and ">" in text:
        return _html_text(text)
    return text


# --- Any URL -------------------------------------------------------------------------------------


class _TextFromHtml(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg", "form"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skipping = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3") and not self.skipping:
            self.out.append("#" * int(tag[1]) + " ")
        if tag == "li" and not self.skipping:
            self.out.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skipping:
            self.out.append(data)


def _html_text(markup: str) -> str:
    parser = _TextFromHtml()
    parser.feed(markup)
    text = html.unescape("".join(parser.out))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*", "\n\n", text).strip()


def import_url(root: Path, url: str, *, folder: str = "manuals", title: str = "", license: str = "",
               transport: httpx.BaseTransport | None = None, progress: Callable[[str], None] = print) -> str:
    """One document from the web: PDFs are kept as PDFs; HTML and text become Markdown."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ImportProblem("Give an http(s) URL")
    if folder not in ("manuals", "templates", "protocols", "reference"):
        raise ImportProblem("folder must be manuals, templates, protocols or reference")
    buffer = io.BytesIO()
    with _client(120, transport) as client:
        with client.stream("GET", url) as response:
            if response.is_error:
                raise ImportProblem(f"{url} returned {response.status_code}")
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            for chunk in response.iter_bytes():
                buffer.write(chunk)
                if buffer.tell() > MAX_DOWNLOAD_BYTES:
                    raise ImportProblem(f"{url} is larger than {MAX_DOWNLOAD_BYTES // 2**20} MB")
    data = buffer.getvalue()
    name = Path(parsed.path).stem or parsed.netloc
    sources = Sources(root)
    if kind == "application/pdf" or data[:5] == b"%PDF-":
        doc_id = f"{folder}/{slugify(title or name)}.pdf"
        (root / doc_id).parent.mkdir(parents=True, exist_ok=True)
        (root / doc_id).write_bytes(data)
    else:
        text = data.decode("utf-8", errors="replace")
        page_title = title
        if "html" in kind or text.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
            parser = _TextFromHtml()
            parser.feed(text)
            page_title = page_title or parser.title.strip()
            text = _html_text(text)
        page_title = page_title or name
        doc_id = f"{folder}/{slugify(page_title)}.md"
        _write(root, doc_id, "\n".join([f"# {page_title}", "", f"Source: {url}"] + ([f"License: {license}"] if license else []) + ["", text, ""]))
    sources.add(doc_id, source=parsed.netloc, url=url, license=license)
    sources.save()
    progress(f"Imported {url} as {doc_id}")
    return doc_id


# --- Command line ----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m protocol_tool.knowledge_import", description=__doc__.split("\n\n")[0])
    parser.add_argument("--knowledge", help="knowledge folder (default: [knowledge] folder in config.toml)")
    sub = parser.add_subparsers(dest="source", required=True)
    sub.add_parser("opentrons-library", help="every protocol in the Opentrons Protocol Library")
    sub.add_parser("opentrons-docs", help="the Opentrons Python API documentation")
    pio = sub.add_parser("protocols-io", help="public protocols.io protocols matching search terms")
    pio.add_argument("queries", nargs="+")
    pio.add_argument("--limit", type=int, default=20, help="protocols per search (default 20)")
    gh = sub.add_parser("github", help="every Opentrons protocol in a lab's public GitHub repository")
    gh.add_argument("repos", nargs="+", help="owner/name")
    one = sub.add_parser("url", help="one document from the web (kit manual, IFU, paper)")
    one.add_argument("url")
    one.add_argument("--folder", default="manuals", choices=["manuals", "templates", "protocols", "reference"])
    one.add_argument("--title", default="")
    one.add_argument("--license", default="", help="terms to record, e.g. 'CC BY 4.0'")
    args = parser.parse_args(argv)

    root = Path(args.knowledge or load_settings().knowledge.folder)
    try:
        if args.source == "opentrons-library":
            import_opentrons_library(root)
        elif args.source == "opentrons-docs":
            import_opentrons_docs(root)
        elif args.source == "protocols-io":
            import_protocols_io(root, args.queries, limit=args.limit)
        elif args.source == "github":
            for repo in args.repos:
                import_github(root, repo)
        else:
            import_url(root, args.url, folder=args.folder, title=args.title, license=args.license)
    except (ImportProblem, httpx.HTTPError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
