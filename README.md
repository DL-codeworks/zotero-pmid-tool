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
- Microsoft Word with the Zotero add-on
- Zotero 10 or newer, open while the tool runs
- In Zotero: **Settings → Advanced → Allow other applications on this computer to communicate with Zotero**
- On first use, choose **Always Allow** when Zotero asks for permission

## Use

1. Open `ZoteroPMIDTool.exe`.
2. Select a `.docx` document.
3. Choose an output folder.
4. Optionally choose a Zotero collection, RIS export, or conversion report.
5. Click **Convert Citations**.
6. Open the newly created DOCX.

## Output

A new copy of the DOCX is created; the original document is left untouched. RIS and report files are optional.

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
