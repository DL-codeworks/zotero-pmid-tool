# Zotero PMID Tool

Convert PMID and DOI references in Microsoft Word `.docx` documents into **live, editable Zotero citations**.

The tool works directly with your Zotero library and creates a new DOCX without overwriting the original.

## What it does

- **Citations:** Recognizes 6–8 digit PMIDs and DOI references in text, in parentheses, and in Word comments, then replaces them with fully functional Zotero citations.
- **Reference management:** Automatically recognizes references already in Zotero and imports missing references.
- **Organization:** Put all references used in the document into a specific Zotero collection, or leave them in My Library.
- **Optional export:** Create a RIS import file for use with other reference-management software.

## Requirements

For normal Windows use:

- Windows
- Microsoft Word
- Zotero 10 or newer
- Zotero Word integration installed
- Zotero open while the conversion runs
- Zotero local API/developer access enabled: **Settings → Advanced → Allow other applications on this computer to communicate with Zotero**
- On the first Zotero write, accept Zotero's authorization popup; choosing **Always Allow** avoids repeated prompts

On first launch, the GUI shows a short requirements notice:

1. In Zotero, go to **Settings → Advanced** and enable **Allow other applications on this computer to communicate with Zotero**.
2. On the first run that needs Zotero write access, Zotero will show a permission popup. Choose **Always Allow**.

The notice also reminds you that the tool creates a new copy and never overwrites the original DOCX. You can choose **Don't show this again**. If you use the packaged EXE, Python is not required.

## GUI use

1. Start `ZoteroPMIDTool.exe`.
2. Choose the original `.docx` manuscript. The output folder defaults to the same folder as the input.
3. Change the output folder if desired.
4. Use the checkboxes to choose whether to also create the PubMed RIS backup and/or conversion report.
5. To organize the document's references in Zotero, check **Put references in a collection/folder** and type the collection name or path directly in the main window.
6. Click **Convert Citations**.
7. If unresolved references require a decision, respond to that prompt in the window.
8. Open the generated `_zotero_citations_YYYYMMDD_HHMMSS.docx`.

The GUI uses a dark theme by default. While conversion is running, the Progress panel reminds you to check Zotero for a permissions popup if the tool appears to be waiting.

## Collection behavior

Collection assignment is configured directly in the main GUI rather than through a popup:

- leave **Put references in a collection/folder** unchecked to leave collection membership unchanged;
- check it and type an existing collection name or full path to use that collection;
- a unique existing match is accepted automatically;
- if there is no match, the tool creates a new top-level My Library collection with the typed name;
- if the text matches more than one existing collection, conversion stops with the matching paths so you can enter a more specific/full path instead of the tool guessing.

The selected collection applies to resolved references used by the document, including references that were already present in Zotero.

## Output

The live-citation DOCX is always created. The RIS and report are optional GUI checkboxes. A run can create:

```text
My Chapter_zotero_citations_20260911_123456.docx
My Chapter_zotero_citations_20260911_123456_REPORT.txt
My Chapter_pubmed_citations_20260911_123456.ris
```

The RIS file is a backup/export. It is not required for normal citation conversion when Zotero resolution/import succeeds.

## Source-code use

The core engine is:

```text
pmid_docx_to_zotero.py
```

Run it directly:

```bat
py pmid_docx_to_zotero.py "C:\path\to\chapter.docx"
```

CLI output controls are also available:

```bat
py pmid_docx_to_zotero.py "C:\path\to\chapter.docx" --output-dir "C:\path\to\output" --collection "Book Chapter" --no-ris --no-report
```

The GUI is:

```text
gui.py
```

It deliberately uses the same core engine rather than maintaining a second citation implementation.

## Build the Windows EXE

On Windows, double-click:

```text
build_exe.bat
```

The build script installs PyInstaller if needed and creates:

```text
dist\ZoteroPMIDTool.exe
```

Equivalent command:

```bat
py -m pip install pyinstaller
py -m PyInstaller --noconfirm --clean --onefile --windowed --name ZoteroPMIDTool gui.py
```

The resulting EXE bundles Python, Tkinter, the GUI, and the citation engine. End users do not need Python installed.

## GitHub Actions build

The included workflow:

```text
.github/workflows/build-windows.yml
```

builds the Windows executable on GitHub and uploads `ZoteroPMIDTool.exe` as a workflow artifact. It can also be started manually from the **Actions** tab.

## Safety / document behavior

- The input DOCX is never overwritten.
- Unresolved identifiers are left unchanged when you choose **Ignore and finish**.
- The tool writes Word citation fields directly; ODF/DOCX Scan is not required.

## Development

The project intentionally uses the Python standard library for runtime functionality. PyInstaller is only a build dependency.

Before committing changes, at minimum run:

```bat
py -m py_compile pmid_docx_to_zotero.py gui.py
```

For citation-field changes, test on a disposable DOCX and verify that Word/Zotero recognizes the generated fields with **Add/Edit Citation**.
