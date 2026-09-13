# Zotero PMID Tool

A Windows tool for managing Zotero references in `.docx` documents. It automatically imports PubMed PMID and DOI references into Zotero and replaces the identifiers in the document with fully functional Zotero citations.

The original document is never overwritten.

## Features

- **Citations:** Recognizes 6–8 digit PMIDs and DOI references in text, parentheses, and Word comments, then replaces them with fully functional Zotero citations.
- **Reference management:** Recognizes references already in Zotero and imports missing references automatically.
- **Collections:** Put all references used in the document into a specific Zotero collection, or leave them in My Library.
- **Optional export:** Create a RIS file for use with other reference-management software.

## Requirements

- Windows
- Microsoft Word
- Zotero 10 or newer
- Zotero Word integration installed
- Zotero open while the tool runs
- In Zotero: **Settings → Advanced → Allow other applications on this computer to communicate with Zotero**
- On first use, choose **Always Allow** when Zotero asks for permission

The packaged EXE does not require Python.

## Use

1. Open `ZoteroPMIDTool.exe`.
2. Select a `.docx` document.
3. Choose an output folder.
4. Optionally choose a Zotero collection, RIS export, or conversion report.
5. Click **Convert Citations**.
6. Open the newly created DOCX.

Unresolved references can be ignored so the rest of the document still finishes.

## Output

The converted DOCX is always created. RIS and report files are optional.

```text
My Chapter_zotero_citations_20260911_123456.docx
My Chapter_zotero_citations_20260911_123456_REPORT.txt
My Chapter_pubmed_citations_20260911_123456.ris
```

## Source

Core engine: `pmid_docx_to_zotero.py`  
GUI: `gui.py`

Run from source:

```bat
py pmid_docx_to_zotero.py "C:\path\to\chapter.docx"
```

## Build

Run `build_exe.bat`, or:

```bat
py -m pip install pyinstaller
py -m PyInstaller --noconfirm --clean --onefile --windowed --name ZoteroPMIDTool gui.py
```

The executable is created at `dist\ZoteroPMIDTool.exe`.
