Knowledge the AI assistant looks things up in while it designs experiments.

  manuals\     kit manuals and instructions for use (IFUs): PDF, .md or .txt
  templates\   assay templates: .md or .txt describing a standard workflow
  protocols\   example protocols from other labs (imported, see below)
  reference\   reference documentation, e.g. the Opentrons Python API (imported)

Drop files in and they are searchable straight away (no restart). The assistant
searches them by keywords (kit name, assay, technique, reagent) and lists which
documents it used in the design's requirements. PDFs need a text layer: scanned
manuals without one can't be searched.

Importing from public sources (run in the protocol-tool folder):

  .venv\Scripts\python -m protocol_tool.knowledge_import opentrons-library
  .venv\Scripts\python -m protocol_tool.knowledge_import opentrons-docs
  .venv\Scripts\python -m protocol_tool.knowledge_import github owner/repo
  .venv\Scripts\python -m protocol_tool.knowledge_import protocols-io "ELISA" "Bradford" --limit 25
  .venv\Scripts\python -m protocol_tool.knowledge_import url https://vendor.com/kit-manual.pdf

Each imported document says where it came from and on what terms; sources.json
lists them all. Running an import again refreshes it.

protocols.io needs a free access token: sign in at https://www.protocols.io/developers,
copy the client access token, and add PROTOCOLS_IO_TOKEN=<token> to protocol-tool\.env.

The two templates in templates\ are generic examples; edit or replace them with the lab's own.
