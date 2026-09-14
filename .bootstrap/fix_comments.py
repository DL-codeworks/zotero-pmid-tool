from pathlib import Path
import textwrap

path = Path('pmid_docx_to_zotero.py')
text = path.read_text(encoding='utf-8')

start = text.index('def comment_records(')
end = text.index('\ndef insert_before', start)
new_comment_records = textwrap.dedent(r'''
def comment_visible_text(body):
    """Extract comment text without concatenating separate Word paragraphs."""
    paragraphs = []
    for pm in PARA_RE.finditer(body):
        value = visible_text(pm.group(0)).strip()
        if value:
            paragraphs.append(value)
    if paragraphs:
        return "\n".join(paragraphs)
    return visible_text(body)


def comment_records(comments_xml, comments_extended_xml):
    parent_by_para = {}
    for m in re.finditer(r'<w15:commentEx\b[^>]*\bw15:paraId="([0-9A-Fa-f]{8})"[^>]*>', comments_extended_xml):
        tag = m.group(0)
        p = re.search(r'\bw15:paraIdParent="([0-9A-Fa-f]{8})"', tag)
        parent_by_para[m.group(1).upper()] = p.group(1).upper() if p else None
    records = []
    for m in COMMENT_RE.finditer(comments_xml):
        body = m.group("body")
        para_ids = re.findall(r'\bw14:paraId="([0-9A-Fa-f]{8})"', body)
        if not para_ids:
            continue
        last_para = para_ids[-1].upper()
        records.append({
            "id": int(m.group("id")),
            "text": comment_visible_text(body),
            "last_para": last_para,
            "is_reply": bool(parent_by_para.get(last_para)),
        })
    return records
''').lstrip()
text = text[:start] + new_comment_records + text[end:]

start = text.index('def append_refs_at_comment_anchor(')
end = text.index('\ndef add_comment_citations_and_replies', start)
new_append = textwrap.dedent(r'''
_CITATION_INSTR_RE = re.compile(
    r'(<w:instrText\b[^>]*>\s*ADDIN ZOTERO_ITEM CSL_CITATION\s+)'
    r'(?P<payload>[\s\S]*?)'
    r'(\s*</w:instrText>)'
)
_FIELD_BEGIN_RE = re.compile(r'<w:fldChar\b[^>]*\bw:fldCharType="begin"[^>]*/?>')
_FIELD_END_RE = re.compile(r'<w:fldChar\b[^>]*\bw:fldCharType="end"[^>]*/?>')


def _zotero_citation_fields(document_xml):
    fields = []
    for instr in _CITATION_INSTR_RE.finditer(document_xml):
        begin = None
        for candidate in _FIELD_BEGIN_RE.finditer(document_xml, 0, instr.start()):
            begin = candidate
        if begin is None:
            continue
        if _FIELD_END_RE.search(document_xml, begin.end(), instr.start()) is not None:
            continue
        end = _FIELD_END_RE.search(document_xml, instr.end())
        if end is None:
            continue
        fields.append({
            "start": begin.start(),
            "end": end.end(),
            "payload_start": instr.start("payload"),
            "payload_end": instr.end("payload"),
        })
    return fields


def _comment_paragraph_bounds(document_xml, start_pos, end_pos):
    para_start = 0
    for pm in re.finditer(r'<w:p\b[^>]*>', document_xml[:start_pos]):
        para_start = pm.start()
    close = document_xml.find('</w:p>', end_pos)
    para_end = len(document_xml) if close < 0 else close + len('</w:p>')
    return para_start, para_end


def _field_for_comment_anchor(document_xml, range_start, range_end):
    fields = _zotero_citation_fields(document_xml)
    if not fields:
        return None
    overlapping = [f for f in fields if f["start"] < range_end and f["end"] > range_start]
    if overlapping:
        return min(overlapping, key=lambda f: abs(f["start"] - range_start) + abs(f["end"] - range_end))
    para_start, para_end = _comment_paragraph_bounds(document_xml, range_start, range_end)
    candidates = []
    for field in fields:
        if field["start"] < para_start or field["end"] > para_end:
            continue
        if field["end"] <= range_start:
            gap = document_xml[field["end"]:range_start]
            distance = range_start - field["end"]
        elif field["start"] >= range_end:
            gap = document_xml[range_end:field["start"]]
            distance = field["start"] - range_end
        else:
            continue
        if re.search(r'[A-Za-z0-9_]', visible_text(gap)):
            continue
        candidates.append((distance, field))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _merge_refs_into_citation_field(document_xml, field, refs):
    raw_payload = html.unescape(document_xml[field["payload_start"]:field["payload_end"]]).strip()
    try:
        citation_data = json.loads(raw_payload)
    except Exception:
        return document_xml, 0
    citation_items = citation_data.get("citationItems")
    if not isinstance(citation_items, list):
        return document_xml, 0
    existing_blob = json.dumps(citation_items, ensure_ascii=False)
    added = 0
    for ref in refs:
        if ref.uri in existing_blob or ref.key in existing_blob:
            continue
        citation_items.append({"uris": [ref.uri]})
        existing_blob += ref.uri + ref.key
        added += 1
    if not added:
        return document_xml, 0
    new_payload = escape_xml_text(json.dumps(citation_data, ensure_ascii=False, separators=(",", ":")))
    document_xml = document_xml[:field["payload_start"]] + new_payload + document_xml[field["payload_end"]:]
    return document_xml, added


def append_refs_at_comment_anchor(document_xml, comment_id, refs):
    """Add comment-requested refs, merging them into the citation the comment targets."""
    cid = str(comment_id)
    start_re = re.compile(r'<w:commentRangeStart\b[^>]*\bw:id="' + re.escape(cid) + r'"[^>]*/>')
    end_re = re.compile(r'<w:commentRangeEnd\b[^>]*\bw:id="' + re.escape(cid) + r'"[^>]*/>')
    sm = start_re.search(document_xml)
    if not sm:
        return document_xml, 0
    em = end_re.search(document_xml, sm.end())
    if not em:
        return document_xml, 0
    anchored_xml = document_xml[sm.end():em.start()]
    refs_to_add = []
    for ref in refs:
        if ref.uri in anchored_xml or ref.key in anchored_xml:
            continue
        if all(x.key != ref.key for x in refs_to_add):
            refs_to_add.append(ref)
    if not refs_to_add:
        return document_xml, 0
    field = _field_for_comment_anchor(document_xml, sm.end(), em.start())
    if field is not None:
        merged_xml, merged_count = _merge_refs_into_citation_field(document_xml, field, refs_to_add)
        if merged_count:
            return merged_xml, merged_count
    preceding_text = list(WT_RE.finditer(document_xml, 0, em.start()))
    run_properties = _run_properties_for_text_match(document_xml, preceding_text[-1]) if preceding_text else ""
    payload = _styled_word_run('<w:t xml:space="preserve"> </w:t>', run_properties) + build_word_field_xml(refs_to_add, run_properties)
    document_xml = document_xml[:em.start()] + payload + document_xml[em.start():]
    return document_xml, len(refs_to_add)
''').lstrip()
text = text[:start] + new_append + text[end:]

fn_start = text.index('def all_identifiers_in_docx(')
fn_end = text.index('\ndef docx_has_zotero_preferences', fn_start)
fn = text[fn_start:fn_end]
old_call = 'collect_from_text(visible_text(cm.group("body")))'
new_call = 'collect_from_text(comment_visible_text(cm.group("body")))'
if old_call not in fn:
    raise SystemExit('Could not find comment identifier call')
fn = fn.replace(old_call, new_call, 1)
text = text[:fn_start] + fn + text[fn_end:]

path.write_text(text, encoding='utf-8')

root = Path('.bootstrap')
for part in root.glob('core.part*'):
    part.unlink()
chunk = 9000
for i in range(0, len(text), chunk):
    (root / 'core.part{:02d}'.format(i // chunk)).write_text(text[i:i + chunk], encoding='utf-8')
