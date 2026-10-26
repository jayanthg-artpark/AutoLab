"""Knowledge imports against fake GitHub, protocols.io and web servers."""

import io
import json
import zipfile

import httpx
import pytest

from protocol_tool.knowledge import Knowledge
from protocol_tool.knowledge_import import (
    ImportProblem, import_github, import_opentrons_docs, import_opentrons_library, import_protocols_io, import_url,
)

quiet = lambda *_: None  # noqa: E731


def library_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("Protocols-develop/protocols/bradford/README.md", "# Bradford Assay\n\n### Author\nOpentrons\n\n## Description\nBSA standards, read at 595 nm.")
        z.writestr("Protocols-develop/protocols/bradford/bradford.ot2.apiv2.py", "def run(ctx):\n    p300 = ctx.load_instrument('p300_single_gen2', 'left')\n")
        z.writestr("Protocols-develop/protocols/bradford/fields.json", '[{"name": "samples", "default": 8}]')
        z.writestr("Protocols-develop/protocols/old-v1/README.md", "# Old")
        z.writestr("Protocols-develop/protocols/old-v1/old-v1.ot2.py", "from opentrons import robot")
        z.writestr("Protocols-develop/protocols/draft/README.md", "# Draft")
        z.writestr("Protocols-develop/protocols/draft/draft.ot2.apiv2.py", "def run(ctx): pass")
        z.writestr("Protocols-develop/protocols/draft/.hide-from-search", "")
    return buffer.getvalue()


def test_opentrons_library(tmp_path):
    data = library_zip()
    count = import_opentrons_library(tmp_path, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data)), progress=quiet)
    assert count == 1  # API v1 and hidden protocols are left out
    doc = (tmp_path / "protocols/opentrons-library/bradford.md").read_text(encoding="utf-8")
    assert doc.startswith("# Bradford Assay\n\nSource: Opentrons Protocol Library, https://library.opentrons.com/p/bradford")
    assert "Robot: OT-2" in doc and "on the Flex use Flex pipettes" in doc
    assert '"default": 8' in doc and "p300_single_gen2" in doc

    knowledge = Knowledge(tmp_path)
    assert knowledge.search("bradford 595")[0][0].doc == "protocols/opentrons-library/bradford.md"
    assert "1 example protocols under protocols/" in knowledge.describe()
    assert json.loads((tmp_path / "sources.json").read_text(encoding="utf-8"))["protocols/opentrons-library/bradford.md"]["robot"] == "OT-2"


def test_opentrons_docs(tmp_path):
    files = {
        "docs/python-api/docs/building-block-commands/liquids.md": "---\ntitle: Liquid Control\n---\n\naspirate() and dispense().",
        "docs/python-api/docs/pipettes.md": "# Pipettes\n\nLoad a Flex pipette with load_instrument.",
        "docs/python-api/docs/img/deck.png": "",
        "api/docs/v2/example_protocols/dilution_tutorial_flex.py": "requirements = {'robotType': 'Flex'}\ndef run(protocol): pass",
        "app/README.md": "# not docs",
    }

    def github(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"truncated": False, "tree": [{"path": p, "type": "blob"} for p in files]})
        return httpx.Response(200, text=files[request.url.path.removeprefix("/Opentrons/opentrons/edge/")])

    assert import_opentrons_docs(tmp_path, transport=httpx.MockTransport(github), progress=quiet) == 3
    page = (tmp_path / "reference/opentrons-api/building-block-commands-liquids.md").read_text(encoding="utf-8")
    assert page.startswith("# Liquid Control\n\nSource: Opentrons Python Protocol API documentation, "
                           "https://docs.opentrons.com/python-api/building-block-commands/liquids/")
    assert "Apache-2.0" in page and "title:" not in page
    assert (tmp_path / "reference/opentrons-api/pipettes.md").read_text(encoding="utf-8").startswith("# Pipettes")
    example = (tmp_path / "reference/opentrons-api/example-dilution-tutorial-flex.md").read_text(encoding="utf-8")
    assert example.startswith("# Official example protocol: dilution_tutorial_flex (Flex)") and "robotType" in example


def test_protocols_io(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTOCOLS_IO_TOKEN", raising=False)
    monkeypatch.setattr("protocol_tool.knowledge_import.load_key_file", lambda name: None)
    with pytest.raises(ImportProblem, match="PROTOCOLS_IO_TOKEN"):
        import_protocols_io(tmp_path, ["elisa"], progress=quiet)

    def api(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        if request.url.path == "/api/v3/protocols":
            assert request.url.params["filter"] == "public" and request.url.params["key"] == "elisa"
            return httpx.Response(200, json={"items": [{"id": 42, "title": "Sandwich ELISA", "uri": "sandwich-elisa-abc"}], "status_code": 0})
        if request.url.path == "/api/v4/protocols/42":
            return httpx.Response(200, json={"status_code": 0, "payload": {
                "title": "Sandwich ELISA", "doi": "dx.doi.org/10.17504/protocols.io.abc", "uri": "sandwich-elisa-abc",
                "authors": [{"name": "A. Researcher"}],
                "description": json.dumps({"blocks": [{"text": "Detects IL-6."}], "entityMap": {}}),
                "steps": [
                    {"section": "Coating", "step": "Coat with **2 ug/mL** capture antibody."},
                    {"section": "Coating", "step": "<p>Incubate overnight at 4 &deg;C.</p>"},
                    {"section": "Detection", "components": [{"source": {"body": "Read at 450 nm."}}]},
                ],
            }})
        return httpx.Response(404)

    assert import_protocols_io(tmp_path, ["elisa"], token="tok", transport=httpx.MockTransport(api), progress=quiet) == 1
    doc = (tmp_path / "protocols/protocols-io/42-sandwich-elisa.md").read_text(encoding="utf-8")
    assert "Source: protocols.io, https://www.protocols.io/view/sandwich-elisa-abc (doi:10.17504/protocols.io.abc)" in doc
    assert "Authors: A. Researcher" in doc and "CC BY 4.0" in doc
    assert "Detects IL-6." in doc
    assert "### Coating\n1. Coat with **2 ug/mL** capture antibody.\n2. Incubate overnight at 4 °C." in doc
    assert "### Detection\n3. Read at 450 nm." in doc


def test_github_repository(tmp_path):
    files = {
        "dna/normalise.py": "from opentrons import protocol_api\nmetadata = {'protocolName': 'DNA normalisation'}\n"
                            "requirements = {'robotType': 'Flex'}\ndef run(protocol):\n    pass\n",
        "tools/plot.py": "import matplotlib\ndef run(x): pass\n",
        "setup.py": "from setuptools import setup\n",
    }

    def github(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/somelab/ot-protocols":
            return httpx.Response(200, json={"default_branch": "main", "license": {"spdx_id": "MIT"}})
        if request.url.path == "/repos/somelab/ot-protocols/git/trees/main":
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob"} for p in files] + [{"path": "README.md", "type": "blob"}]})
        return httpx.Response(200, text=files[request.url.path.removeprefix("/somelab/ot-protocols/main/")])

    assert import_github(tmp_path, "somelab/ot-protocols", transport=httpx.MockTransport(github), progress=quiet) == 1
    doc = (tmp_path / "protocols/github/somelab-ot-protocols/dna-normalise.md").read_text(encoding="utf-8")
    assert doc.startswith("# DNA normalisation\n\nSource: https://github.com/somelab/ot-protocols/blob/main/dna/normalise.py")
    assert "Robot: Flex" in doc and "License: MIT" in doc
    with pytest.raises(ImportProblem):
        import_github(tmp_path, "not a repo", progress=quiet)


def test_url_import_of_html_and_pdf(tmp_path):
    page = b"<html><head><title>IL-6 Kit IFU</title><script>x()</script></head><body><nav>menu</nav><h1>IL-6 Kit</h1><p>Add 100 &micro;L per well.</p><ul><li>Read at 450 nm</li></ul></body></html>"
    pdf = b"%PDF-1.4 not really a pdf"

    def web(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ifu":
            return httpx.Response(200, content=page, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})

    doc_id = import_url(tmp_path, "https://vendor.test/ifu", license="vendor IFU", transport=httpx.MockTransport(web), progress=quiet)
    assert doc_id == "manuals/il-6-kit-ifu.md"
    text = (tmp_path / doc_id).read_text(encoding="utf-8")
    assert "# IL-6 Kit IFU\n\nSource: https://vendor.test/ifu\nLicense: vendor IFU" in text
    assert "Add 100 µL per well." in text and "- Read at 450 nm" in text
    assert "x()" not in text and "menu" not in text

    assert import_url(tmp_path, "https://vendor.test/docs/kit-manual.pdf", transport=httpx.MockTransport(web), progress=quiet) == "manuals/kit-manual.pdf"
    with pytest.raises(ImportProblem):
        import_url(tmp_path, "file:///etc/passwd", progress=quiet)
