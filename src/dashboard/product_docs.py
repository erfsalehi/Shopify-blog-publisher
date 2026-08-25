"""Downloading a product's documents, and reading what's in them.

A manufacturer's real product information is in the PDFs, not the web page.
The page says "durable luxury vinyl plank"; the spec sheet says 20 mil wear
layer, 5.5 mm thickness, EIR finish, AC5, 30-year residential warranty. Those
are the facts a customer decides on and the ones a search engine can answer
with, so the importer downloads them, reads them, and hands the text to the
copy stage.

Three limits, all of them load-bearing:

  * **`MAX_BYTES` per document.** A full catalogue PDF can be 80 MB of press
    photography. It is not worth downloading on a serverless function with a
    memory ceiling, and the words in it aren't better than the ones in the
    two-page spec sheet.
  * **`MAX_PAGES` read per document.** Specifications live at the front. Page
    40 of an installation manual is a diagram caption.
  * **`MAX_CHARS` of extracted text.** What survives is what the copy stage
    can actually attend to.

Failure here is never fatal. A document that won't download, or is a scan
with no text layer, records why on the doc and the import carries on — a
product with no spec sheet is still a product, and the alternative (failing
the whole run because one PDF 404s) would strand every product behind it.
"""

from __future__ import annotations

import io
import logging
import re
import time
from urllib.parse import unquote, urlparse

import httpx

from dashboard.manufacturer import PAUSE, SourceDoc, client as _default_client

log = logging.getLogger(__name__)

#: 25 MB. Comfortably larger than any spec sheet, small enough that a
#: function reading several of them stays inside its memory limit.
MAX_BYTES = 25 * 1024 * 1024
MAX_PAGES = 12
MAX_CHARS = 20000
#: Documents read per product. Manufacturers link a spec sheet, a warranty,
#: an installation guide and a care guide; past four it's marketing.
MAX_DOCS = 6

_PDF_MAGIC = b"%PDF-"


class DocError(RuntimeError):
    pass


def filename_for(doc: SourceDoc) -> str:
    """A sensible filename for Shopify Files, derived from the source URL.

    Shopify shows this to customers in the download link, so a URL ending
    `/f/9f3a2b?download=1` becomes `spec-sheet.pdf` from the link text rather
    than a hash nobody can read.
    """
    path = unquote(urlparse(doc.url).path)
    name = path.rsplit("/", 1)[-1].strip()
    if not name or "." not in name:
        stem = _slug(doc.title or doc.kind or "document") or "document"
        name = f"{stem}.pdf"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")
    return (name or "document.pdf")[:120]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]


def download(doc: SourceDoc, http: httpx.Client, *, max_bytes: int = MAX_BYTES) -> bytes:
    """Fetch the document, refusing anything oversized or obviously not one.

    Streamed and counted rather than read whole: `content-length` is a claim,
    and a server that under-reports it would otherwise put an unbounded
    response into memory.
    """
    with http.stream("GET", doc.url) as resp:
        if resp.status_code != 200:
            raise DocError(f"HTTP {resp.status_code}")
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise DocError(
                f"{int(declared) // 1024 // 1024} MB is over the "
                f"{max_bytes // 1024 // 1024} MB limit"
            )
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise DocError(
                    f"larger than the {max_bytes // 1024 // 1024} MB limit"
                )
            chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise DocError("the download was empty")
    return data


def is_pdf(data: bytes) -> bool:
    return data[:5] == _PDF_MAGIC


def extract_text(
    data: bytes, *, max_pages: int = MAX_PAGES, max_chars: int = MAX_CHARS
) -> tuple[str, int]:
    """Text and page count from a PDF. `("", 0)` when there's nothing to read.

    pypdf is imported here rather than at module scope so a deployment that
    somehow lacks it still imports and runs the rest of the importer — the
    products come through with their PDFs attached but unread, which is a
    degraded import rather than a broken one.
    """
    if not is_pdf(data):
        raise DocError("not a PDF, so its text wasn't read")
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - depends on the install
        raise DocError(
            "pypdf isn't installed, so PDF text can't be read "
            "(pip install -e '.[dashboard]')"
        ) from e

    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            # An owner-password PDF often still decrypts with an empty user
            # password, which is the common "printing restricted" case.
            try:
                reader.decrypt("")
            except Exception as e:  # noqa: BLE001
                raise DocError("the PDF is password-protected") from e
        pages = list(reader.pages)[:max_pages]
        parts: list[str] = []
        for page in pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception as e:  # noqa: BLE001 - one bad page isn't the file
                log.info("page text extraction failed: %s", e)
        total_pages = len(reader.pages)
    except DocError:
        raise
    except Exception as e:  # noqa: BLE001 - malformed PDFs are common
        raise DocError(f"the PDF couldn't be parsed ({type(e).__name__})") from e

    text = _tidy("\n".join(parts))
    if not text.strip():
        raise DocError(
            "no text layer — this looks like a scan, so nothing could be read "
            "from it"
        )
    return text[:max_chars], total_pages


def _tidy(text: str) -> str:
    """Collapse the whitespace PDF extraction leaves behind.

    Extracted PDF text arrives with a newline per rendered line and runs of
    spaces where the layout had columns. Left alone it triples the token
    count for no added meaning.
    """
    text = text.replace(" ", " ").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return re.sub(r" *\n *", "\n", text).strip()


def read_docs(
    docs: list[SourceDoc],
    *,
    http: httpx.Client | None = None,
    limit: int = MAX_DOCS,
    max_bytes: int = MAX_BYTES,
    keep_data: bool = True,
) -> list[SourceDoc]:
    """Download and read up to `limit` documents, in place.

    Ordered by how much a customer's decision depends on them, because the
    limit bites on a page that links a dozen: a spec sheet is worth more than
    a lookbook. Returns the same list for convenience.

    `keep_data` holds the bytes on the doc so the publish step can upload the
    very file whose text it just read. Turn it off for a dry run, where
    nothing is uploaded and holding several PDFs in memory buys nothing.
    """
    own = http is None
    http = http or _default_client()
    try:
        for index, doc in enumerate(_by_priority(docs)):
            if index >= limit:
                doc.error = f"skipped — past the {limit}-document limit"
                continue
            try:
                data = download(doc, http, max_bytes=max_bytes)
                doc.bytes_len = len(data)
                if keep_data:
                    doc.data = data
                doc.text, doc.pages = extract_text(data)
            except DocError as e:
                doc.error = str(e)
                log.info("doc %s: %s", doc.url, e)
            except Exception as e:  # noqa: BLE001 - a bad doc is not a bad run
                doc.error = f"{type(e).__name__}: {e}"[:300]
                log.info("doc %s failed: %s", doc.url, e)
            time.sleep(PAUSE)
        return docs
    finally:
        if own:
            http.close()


#: Most decision-relevant first. Anything unrecognised sorts last.
_PRIORITY = {
    "spec": 0, "installation": 1, "warranty": 2, "maintenance": 3,
    "compliance": 4, "brochure": 5, "other": 6,
}


def _by_priority(docs: list[SourceDoc]) -> list[SourceDoc]:
    return sorted(docs, key=lambda d: _PRIORITY.get(d.kind, 9))


def doc_digest(docs: list[SourceDoc], *, per_doc: int = 6000) -> str:
    """The documents as one block of text for the copy stage.

    Labelled by kind and filename so the model can say "per the warranty"
    rather than attributing a 30-year residential warranty to the
    installation guide.
    """
    parts: list[str] = []
    for doc in docs:
        if not doc.text:
            continue
        label = f"{doc.kind.upper()} — {doc.title or filename_for(doc)}"
        parts.append(f"### {label}\n{doc.text[:per_doc]}")
    return "\n\n".join(parts)
