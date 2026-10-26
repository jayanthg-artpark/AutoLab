"""Knowledge the assistant can look things up in.

Files in the knowledge folder (by default protocol-tool/knowledge):

  manuals/    kit manuals and instructions for use: PDF, Markdown or text
  templates/  assay templates: Markdown or text describing a standard workflow
  protocols/  example protocols from other labs (Opentrons Protocol Library, protocols.io, ...)
  reference/  reference documentation (the Opentrons Python API, ...)

are split into passages and searched by keyword (no AI needed to search; the
assistant decides what to look up and what to make of it). The index is
rebuilt whenever a file is added, changed or removed. knowledge_import.py
fills protocols/ and reference/ from public sources; sources.json records
where each imported document came from and under what terms.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

FOLDERS = {
    "manuals": "Kit manual", "templates": "Assay template",
    "protocols": "Example protocol", "reference": "Reference",
}
SUFFIXES = {".pdf", ".md", ".txt", ".py"}
SOURCES_FILE = "sources.json"
PASSAGE_CHARS = 1500
MAX_READ_CHARS = 12000
WORD = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*")
STOP = set("the a an and or of to in on for with at by is are be as it this that from into per use used".split())


@dataclass
class Passage:
    doc: str  # "manuals/elisa-kit.pdf"
    page: int  # 1-based for PDFs, 0 for text files
    text: str
    terms: Counter = field(default_factory=Counter)


@dataclass
class Document:
    id: str  # path under the knowledge folder, e.g. "templates/elisa.md"
    kind: str
    title: str
    pages: int
    chars: int


def _terms(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 1]


class Knowledge:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        self._signature: tuple = ()
        self._docs: dict[str, Document] = {}
        self._passages: list[Passage] = []
        self._df: Counter = Counter()
        self._sources: dict[str, dict] = {}

    # --- Index ------------------------------------------------------------------------

    def _files(self) -> list[Path]:
        files = []
        for folder in FOLDERS:
            base = self.root / folder
            if base.is_dir():
                files += [p for p in sorted(base.rglob("*")) if p.is_file() and p.suffix.lower() in SUFFIXES]
        return files

    def _refresh(self) -> None:
        files = self._files()
        manifest = self.root / SOURCES_FILE
        signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files + ([manifest] if manifest.exists() else []))
        if signature == self._signature:
            return
        try:
            self._sources = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        except ValueError:
            log.warning("Knowledge: %s is not valid JSON; ignoring it", manifest)
            self._sources = {}
        docs, passages = {}, []
        for path in files:
            doc_id = path.relative_to(self.root).as_posix()
            try:
                pages = _read_pages(path)
            except Exception as exc:
                log.warning("Knowledge: could not read %s: %s", doc_id, exc)
                continue
            title = _title(path, pages)
            docs[doc_id] = Document(doc_id, FOLDERS[doc_id.split("/")[0]], title, len(pages), sum(map(len, pages)))
            for number, text in enumerate(pages, start=1 if path.suffix.lower() == ".pdf" else 0):
                for chunk in _chunks(text):
                    passages.append(Passage(doc_id, number, chunk, Counter(_terms(chunk + " " + title))))
        df: Counter = Counter()
        for p in passages:
            df.update(set(p.terms))
        self._docs, self._passages, self._df, self._signature = docs, passages, df, signature
        log.info("Knowledge: %d documents, %d passages", len(docs), len(passages))

    # --- Queries ----------------------------------------------------------------------

    def documents(self) -> list[Document]:
        with self._lock:
            self._refresh()
            return list(self._docs.values())

    def title(self, doc_id: str) -> str:
        doc = self._docs.get(doc_id)
        return doc.title if doc else doc_id

    def search(self, query: str, limit: int = 5, per_doc: int = 2) -> list[tuple[Passage, float]]:
        """Passages that best match the query (BM25), at most `per_doc` from any one document."""
        with self._lock:
            self._refresh()
            words = set(_terms(query))
            if not words or not self._passages:
                return []
            n = len(self._passages)
            avg = sum(sum(p.terms.values()) for p in self._passages) / n
            scored = []
            for p in self._passages:
                length = sum(p.terms.values())
                score = 0.0
                for w in words:
                    tf = p.terms.get(w, 0)
                    if not tf:
                        continue
                    idf = math.log(1 + (n - self._df[w] + 0.5) / (self._df[w] + 0.5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg))
                if score > 0:
                    scored.append((p, score))
            scored.sort(key=lambda item: item[1], reverse=True)
            hits, per = [], Counter()
            for passage, score in scored:
                if per[passage.doc] < per_doc:
                    per[passage.doc] += 1
                    hits.append((passage, score))
                    if len(hits) == limit:
                        break
            return hits

    def read(self, doc_id: str, page: int | None = None) -> str:
        """A document's text (one page of a PDF if `page` is given), cut to a readable length."""
        with self._lock:
            self._refresh()
            if doc_id not in self._docs:
                raise KeyError(doc_id)
            source = self._sources.get(doc_id)
        path = (self.root / doc_id).resolve()
        if self.root.resolve() not in path.parents:
            raise KeyError(doc_id)
        pages = _read_pages(path)
        if page is not None:
            if not 1 <= page <= len(pages):
                raise ValueError(f"{doc_id} has {len(pages)} pages")
            text = pages[page - 1]
        else:
            text = "\n\n".join(f"[page {i}]\n{t}" if len(pages) > 1 else t for i, t in enumerate(pages, start=1))
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + f"\n\n[... cut at {MAX_READ_CHARS} characters; read one page at a time]"
        if source and source.get("url") and source["url"] not in text[:2000]:
            text = f"Source: {source['url']}" + (f" ({source['license']})" if source.get("license") else "") + f"\n\n{text}"
        return text

    def describe(self) -> str:
        """What is available, for the assistant's design-state block.

        Manuals and templates are listed by name; the (many) example protocols and
        reference pages are counted, and found with search_knowledge."""
        docs = self.documents()
        if not docs:
            return "none (the lab has not added kit manuals, assay templates or example protocols)"
        named = [d for d in docs if d.id.split("/")[0] in ("manuals", "templates")]
        parts = [f"{d.id} ({d.kind}: {d.title})" for d in named[:40]]
        if len(named) > 40:
            parts.append(f"... and {len(named) - 40} more manuals and templates")
        for folder in ("protocols", "reference"):
            count = sum(1 for d in docs if d.id.startswith(folder + "/"))
            if count:
                parts.append(f"{count} {FOLDERS[folder].lower()}{'s' if folder == 'protocols' else ' pages'} under {folder}/ (search them)")
        return "; ".join(parts)


def _read_pages(path: Path) -> list[str]:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return [(page.extract_text() or "").strip() for page in PdfReader(str(path)).pages]
    return [path.read_text(encoding="utf-8", errors="replace")]


def _title(path: Path, pages: list[str]) -> str:
    for line in (pages[0] if pages else "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:100]
    return path.stem


def _chunks(text: str) -> list[str]:
    """Passages of about PASSAGE_CHARS, split at paragraph breaks where possible."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        while len(para) > PASSAGE_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:PASSAGE_CHARS])
            para = para[PASSAGE_CHARS:]
        if len(current) + len(para) + 2 > PASSAGE_CHARS and current:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks
