"""Knowledge imports against fake GitHub, protocols.io and web servers."""

import io
import json
import zipfile

import httpx
import pytest

from protocol_tool.knowledge import Knowledge
from protocol_tool.knowledge_import import (
    ImportProblem, import_github, import_library_folder, import_opentrons_docs, import_opentrons_labware, import_opentrons_library,
    import_protocols_io, import_url,
)

quiet = lambda *_: None  # noqa: E731


def library_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("Protocols-develop/protocols/bradford/README.md", "# Bradford Assay\n\n### Author\nOpentrons\n\n## Description\nBSA standards, read at 595 nm.")
        z.writestr("Protocols-develop/protocols/bradford/bradford.flex.py", "requirements = {'robotType': 'Flex'}\ndef run(ctx):\n    p = ctx.load_instrument('flex_1channel_1000', 'left')\n")
        z.writestr("Protocols-develop/protocols/bradford/fields.json", '[{"name": "samples", "default": 8}]')
        z.writestr("Protocols-develop/protocols/elisa/README.md", "# ELISA")
        z.writestr("Protocols-develop/protocols/elisa/elisa.ot2.apiv2.py", "def run(ctx):\n    p300 = ctx.load_instrument('p300_single_gen2', 'left')\n")
        z.writestr("Protocols-develop/protocols/old-v1/README.md", "# Old")
        z.writestr("Protocols-develop/protocols/old-v1/old-v1.ot2.py", "from opentrons import robot")
        z.writestr("Protocols-develop/protocols/draft/README.md", "# Draft")
        z.writestr("Protocols-develop/protocols/draft/draft.ot2.apiv2.py", "def run(ctx): pass")
        z.writestr("Protocols-develop/protocols/draft/.hide-from-search", "")
    return buffer.getvalue()


def test_opentrons_library(tmp_path):
    (tmp_path / "protocols/opentrons-library").mkdir(parents=True)
    (tmp_path / "protocols/opentrons-library/old-ot2-protocol.md").write_text("# gone", encoding="utf-8")
    data = library_zip()
    count = import_opentrons_library(tmp_path, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data)), progress=quiet)
    assert count == 1  # protocols for other robots, API v1 and hidden protocols are left out
    assert sorted(p.name for p in (tmp_path / "protocols/opentrons-library").iterdir()) == ["bradford.md"]
    doc = (tmp_path / "protocols/opentrons-library/bradford.md").read_text(encoding="utf-8")
    assert doc.startswith("# Bradford Assay\n\nSource: Opentrons Protocol Library, https://library.opentrons.com/p/bradford")
    assert "Robot: Flex" in doc
    assert '"default": 8' in doc and "flex_1channel_1000" in doc

    knowledge = Knowledge(tmp_path)
    assert knowledge.search("bradford 595")[0][0].doc == "protocols/opentrons-library/bradford.md"
    assert "1 example protocols under protocols/" in knowledge.describe()
    assert json.loads((tmp_path / "sources.json").read_text(encoding="utf-8"))["protocols/opentrons-library/bradford.md"]["robot"] == "Flex"


def test_opentrons_docs(tmp_path):
    files = {
        "docs/python-api/docs/building-block-commands/liquids.md": "---\ntitle: Liquid Control\n---\n\naspirate() and dispense().",
        "docs/python-api/docs/pipettes.md": (
            "# Pipettes\n\nLoad a Flex pipette with load_instrument. On an OT-2, use a GEN2 pipette.\n\n"
            '=== "Flex Pipettes"\n    flex_1channel_1000\n\n=== "OT-2 Pipettes"\n    p300_single_gen2\n\n'
            "- **Flex:** requirements are required.\n- **OT-2:** requirements are optional.\n    ```python\n"
            '    requirements = {"robotType": "OT-2"}\n    ```\n\n'
            "| Pipette | Load name |\n|---|---|\n| Flex 1-Channel | `flex_1channel_1000` |\n| P300 | `p300_single_gen2` |\n\n"
            "```python\ntips = protocol.load_labware('opentrons_96_tiprack_300ul', 1)\n```\n\n"
            "## Loading OT-2 pipettes\n\nGEN2 pipettes.\n\n## Next\n\nDone."
        ),
        "docs/python-api/docs/adapting-ot2-flex.md": "# Adapting OT-2 protocols for Flex",
        "docs/python-api/docs/modules/magnetic-module.md": "# Magnetic Module\n\n## OT-2 only\n\nThe Magnetic Module works on the OT-2.",
        "api/docs/v2/example_protocols/dilution_tutorial.py": "metadata = {'apiLevel': '2.16'}\ndef run(protocol): pass",
        "docs/python-api/docs/img/deck.png": "",
        "api/docs/v2/example_protocols/dilution_tutorial_flex.py": "requirements = {'robotType': 'Flex'}\ndef run(protocol): pass",
        "app/README.md": "# not docs",
    }

    def github(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"truncated": False, "tree": [{"path": p, "type": "blob"} for p in files]})
        return httpx.Response(200, text=files[request.url.path.removeprefix("/Opentrons/opentrons/edge/")])

    (tmp_path / "reference/opentrons-api").mkdir(parents=True)
    (tmp_path / "reference/opentrons-api/adapting-ot2-flex.md").write_text("# gone", encoding="utf-8")
    assert import_opentrons_docs(tmp_path, transport=httpx.MockTransport(github), progress=quiet) == 3
    page = (tmp_path / "reference/opentrons-api/building-block-commands-liquids.md").read_text(encoding="utf-8")
    assert page.startswith("# Liquid Control\n\nSource: Opentrons Python Protocol API documentation, "
                           "https://docs.opentrons.com/python-api/building-block-commands/liquids/")
    assert "Apache-2.0" in page and "title:" not in page
    pipettes = (tmp_path / "reference/opentrons-api/pipettes.md").read_text(encoding="utf-8")
    assert pipettes.startswith("# Pipettes")
    assert "Load a Flex pipette with load_instrument." in pipettes and "flex_1channel_1000" in pipettes
    assert "**Flex:** requirements are required." in pipettes and "## Next" in pipettes
    for gone in ("OT-2", "GEN2", "gen2", "300ul", "robotType"):
        assert gone not in pipettes, gone
    # Pages and examples only about the other robot are left out.
    assert sorted(p.name for p in (tmp_path / "reference/opentrons-api").iterdir()) == [
        "building-block-commands-liquids.md", "example-dilution-tutorial-flex.md", "pipettes.md"]
    example = (tmp_path / "reference/opentrons-api/example-dilution-tutorial-flex.md").read_text(encoding="utf-8")
    assert example.startswith("# Official example protocol: dilution_tutorial_flex (Flex)") and "robotType" in example



def test_opentrons_labware(tmp_path):
    (tmp_path / "reference/opentrons-labware").mkdir(parents=True)
    (tmp_path / "reference/opentrons-labware/retired_labware.md").write_text("# gone", encoding="utf-8")

    assert import_opentrons_labware(tmp_path, progress=quiet) > 100
    folder = tmp_path / "reference/opentrons-labware"
    rack = (folder / "opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical.md").read_text(encoding="utf-8")
    assert 'protocol.load_labware("opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical"' in rack
    assert "4 x Falcon 4x50 mL Conical in A3, B3, A4, B4: holds 50 mL" in rack
    assert 'well.meniscus(z=-2, target="end")' in rack  # deep tubes say how to aspirate below the surface
    assert "measure_liquid_height" in (folder / "liquid-height.md").read_text(encoding="utf-8")
    tips = (folder / "opentrons_flex_96_tiprack_50ul.md").read_text(encoding="utf-8")
    assert "Tip length: 57.9 mm" in tips and "meniscus" not in tips
    assert not (folder / "opentrons_96_tiprack_300ul.md").exists()  # only Flex tips fit Flex pipettes
    assert not (folder / "retired_labware.md").exists()
    assert "- nest_96_wellplate_200ul_flat: NEST 96 Well Plate" in (folder / "index.md").read_text(encoding="utf-8")

    hits = Knowledge(tmp_path).search("50 mL Falcon tube rack")
    assert "reference/opentrons-labware/opentrons_6_tuberack_falcon_50ml_conical.md" in [p.doc for p, _ in hits]


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


def test_flex_protocols_downloaded_from_the_library_website(tmp_path):
    download = tmp_path / "download"
    download.mkdir()
    (download / "elisa__ELISA_Flex.py").write_text(
        "metadata = {'protocolName': 'ELISA', 'author': 'Opentrons', 'description': 'Coats a plate.'}\n"
        "requirements = {'robotType': 'Flex', 'apiLevel': '2.20'}\ndef run(protocol):\n    pass\n", encoding="utf-8")
    (download / "old__pcr.py").write_text("metadata = {}\nrequirements = {'robotType': 'OT-3'}\n", encoding="utf-8")
    (download / "ot2__x.py").write_text("metadata = {'apiLevel': '2.13'}\ndef run(ctx): pass\n", encoding="utf-8")
    (download / "manifest.csv").write_text(
        '"name","slug","status","saved_as"\n'
        f'"Invitrogen ELISA: coating","elisa","downloaded","{download / "elisa__ELISA_Flex.py"}"\n'
        '"Gone","gone","unavailable",""\n', encoding="utf-8-sig")
    root = tmp_path / "knowledge"
    assert import_library_folder(root, download, progress=quiet) == 2  # "OT-3" is a Flex too; the OT-2 one is left out
    doc = (root / "protocols/opentrons-flex-library/elisa.md").read_text(encoding="utf-8")
    assert doc.startswith("# Invitrogen ELISA: coating\n") and "https://library.opentrons.com/p/elisa" in doc
    assert "Author: Opentrons" in doc and "Coats a plate." in doc and "def run(protocol):" in doc
    assert (root / "protocols/opentrons-flex-library/old-pcr.md").exists()
    assert Knowledge(root).search("ELISA coating")[0][0].doc == "protocols/opentrons-flex-library/elisa.md"
