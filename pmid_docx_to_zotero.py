#!/usr/bin/env python3
import argparse
import difflib
import html
import json
import os
import random
import socket
import re
import string
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

ZOTERO_API = os.environ.get("ZOTERO_API", "http://localhost:23119/api")
EUTILS_FETCH = os.environ.get("EUTILS_FETCH", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi")
EUTILS_SEARCH = os.environ.get("EUTILS_SEARCH", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi")

PMID_RE = re.compile(r"(?<!\d)(\d{6,8})(?!\d)")
PMID_LABELED_RE = re.compile(r"\b(?:PMID|PubMed(?:\s+ID)?)\s*:?\s*(\d{6,8})\b", re.I)
PUBMED_URL_RE = re.compile(r"https?://(?:www\.)?pubmed\.ncbi\.nlm\.nih\.gov/(\d{6,8})(?:/|$)", re.I)
DOI_RE = re.compile(
    r"(?:(?:https?://(?:dx\.)?doi\.org/)|(?:doi\s*:\s*))?"
    r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.I
)
WT_RE = re.compile(r"(<w:t\b(?![^>]*?/>)[^>]*>)(.*?)(</w:t>)", re.S)
PARA_RE = re.compile(r"<w:p\b[\s\S]*?</w:p>")
COMMENT_RE = re.compile(r'<w:comment\b[^>]*\bw:id="(?P<id>\d+)"[^>]*>(?P<body>[\s\S]*?)</w:comment>')

WRITE_EVENTS = []


def write_event(message):
    stamp = datetime.now().strftime("%H:%M:%S")
    line = "[{}] {}".format(stamp, message)
    WRITE_EVENTS.append(line)
    print(line)


class ZoteroRef(object):
    def __init__(self, key, label, title, uri, data=None, item=None):
        self.key = key
        self.label = label
        self.title = title or ""
        self.uri = uri
        self.data = data or {}
        self.item = item or {}


class Replacement(object):
    def __init__(self, start, end, refs, identifiers, trailing_text=""):
        self.start = start
        self.end = end
        self.refs = refs
        self.identifiers = identifiers
        self.trailing_text = trailing_text


class CollectionSelectionError(RuntimeError):
    pass


def _trim_doi_value(value):
    value = html.unescape(value or "").strip()
    value = value.rstrip(".,;")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1].rstrip()
    while value.endswith("]") and value.count("]") > value.count("["):
        value = value[:-1].rstrip()
    return value


def normalize_doi(value):
    value = html.unescape(value or "").strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi\s*:\s*", "", value, flags=re.I)
    return _trim_doi_value(value).lower()


def visible_text(xml_fragment):
    return "".join(html.unescape(m.group(2)) for m in WT_RE.finditer(xml_fragment))




def _decode_response_body(raw):
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except Exception:
        return text


def zotero_raw_request(method, path, payload=None, api_key=None, server_id=None, extra_headers=None, timeout=20):
    headers = {
        "User-Agent": "PMID-DOCX-to-Zotero/5.0",
        "Zotero-API-Version": "3",
        "Accept": "application/json",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Zotero-API-Key"] = api_key
    if server_id:
        headers["Zotero-Server-ID"] = server_id
    if extra_headers:
        headers.update(extra_headers)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(ZOTERO_API + path, data=data, headers=headers, method=method)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return _decode_response_body(raw), dict(response.headers.items()), response.status, time.monotonic() - started
    except urllib.error.HTTPError as e:
        raw = e.read()
        return _decode_response_body(raw), dict(e.headers.items()), e.code, time.monotonic() - started
    except (socket.timeout, TimeoutError) as e:
        raise RuntimeError("TIMEOUT after {:.1f}s waiting for Zotero {} {}".format(time.monotonic() - started, method, path))
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            raise RuntimeError("TIMEOUT after {:.1f}s waiting for Zotero {} {}".format(time.monotonic() - started, method, path))
        raise RuntimeError("Could not connect to Zotero at localhost:23119. Make sure Zotero is open.\n{}".format(e))


def zotero_server_id():
    obj, headers, status, elapsed = zotero_raw_request("GET", "/")
    if status == 403:
        raise RuntimeError("Enable Zotero -> Settings -> Advanced -> Allow other applications on this computer to communicate with Zotero.")
    if status >= 400:
        raise RuntimeError("Could not read Zotero local API (HTTP {}).".format(status))
    server_id = headers.get("Zotero-Server-ID") or headers.get("zotero-server-id")
    if not server_id:
        return None
    return server_id


def app_data_dir():
    """Persistent per-user storage that also works from a PyInstaller one-file EXE."""
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        folder = Path(base) / "ZoteroPMIDTool"
    else:
        folder = Path.home() / ".zotero_pmid_tool"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return folder


def auth_cache_path():
    return app_data_dir() / "zotero_local_write_auth.json"


def load_cached_write_key(server_id):
    try:
        data = json.loads(auth_cache_path().read_text(encoding="utf-8"))
        if data.get("server_id") == server_id and data.get("key"):
            return data["key"]
    except Exception:
        pass
    return None


def save_cached_write_key(server_id, key):
    try:
        auth_cache_path().write_text(
            json.dumps({"server_id": server_id, "key": key}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def clear_cached_write_key():
    try:
        auth_cache_path().unlink()
    except OSError:
        pass


def authorize_zotero_write(server_id):
    obj, headers, status, elapsed = zotero_raw_request(
        "POST",
        "/local/authorize",
        payload={"appName": "Zotero PMID Tool"},
        server_id=server_id,
        timeout=60,
    )
    if status == 403:
        raise RuntimeError("Zotero write authorization was denied.")
    if status == 404 or status == 405:
        raise RuntimeError("This Zotero version does not support local API writes.")
    if status >= 400 or not isinstance(obj, dict) or not obj.get("key"):
        raise RuntimeError("Could not authorize Zotero writes (HTTP {}): {}".format(status, obj))
    if obj.get("remember"):
        save_cached_write_key(server_id, obj["key"])
    return obj["key"]


def zotero_write_key(server_id):
    return load_cached_write_key(server_id) or authorize_zotero_write(server_id)


def _payload_identifier(item):
    pmids = item_pmids(item)
    if pmids:
        return "PMID {}".format(sorted(pmids, key=int)[0])
    dois = item_dois(item)
    if dois:
        return "DOI {}".format(sorted(dois)[0])
    title = str(item.get("title") or "").strip()
    return title[:80] if title else "reference"


def _find_payload_in_zotero(item):
    pmid_map, doi_map, title_map, _ = load_zotero_index()
    for pmid in item_pmids(item):
        if pmid in pmid_map:
            return pmid_map[pmid]
    for doi in item_dois(item):
        doi = normalize_doi(doi)
        if doi in doi_map:
            return doi_map[doi]
    title = str(item.get("title") or "")
    if title:
        return choose_title_match(title, title_map, year_from_data(item), first_author_from_data(item))
    return None


def post_zotero_items(items, collection_key=None, max_attempts=3, timeout=60):
    """Create items one-by-one with retries and verification after ambiguous failures."""
    if not items:
        return 0, []
    server_id = zotero_server_id()
    if not server_id:
        raise RuntimeError("This Zotero version does not support local API writes.")

    created = 0
    failures = []
    key = zotero_write_key(server_id)

    for original in items:
        item = dict(original)
        if collection_key:
            cols = list(item.get("collections") or [])
            if collection_key not in cols:
                cols.append(collection_key)
            item["collections"] = cols

        label = _payload_identifier(item)
        token = uuid.uuid4().hex
        done = False

        for attempt in range(1, max_attempts + 1):
            write_event("Zotero write {}/{} for {} (timeout {}s)...".format(attempt, max_attempts, label, timeout))
            try:
                obj, response_headers, status, elapsed = zotero_raw_request(
                    "POST",
                    "/users/0/items",
                    payload=[item],
                    api_key=key,
                    server_id=server_id,
                    extra_headers={"Zotero-Write-Token": token},
                    timeout=timeout,
                )
            except RuntimeError as exc:
                message = str(exc)
                write_event("{}".format(message))
                if "TIMEOUT" in message:
                    write_event("The request outcome is ambiguous, so checking Zotero before retrying...")
                    try:
                        found = _find_payload_in_zotero(item)
                    except Exception as verify_exc:
                        found = None
                        write_event("Verification check also failed: {}".format(verify_exc))
                    if found:
                        write_event("{} is present in Zotero; treating the timed-out write as successful.".format(label))
                        created += 1
                        done = True
                        break
                if attempt < max_attempts:
                    delay = 2 ** attempt
                    write_event("Retrying {} in {}s.".format(label, delay))
                    time.sleep(delay)
                    continue
                failures.append((label, message))
                break

            write_event("Zotero answered HTTP {} in {:.2f}s for {}.".format(status, elapsed, label))

            if status == 401:
                write_event("Zotero rejected the cached write authorization; requesting a fresh one.")
                clear_cached_write_key()
                key = authorize_zotero_write(server_id)
                if attempt < max_attempts:
                    continue
                failures.append((label, "HTTP 401 after reauthorization"))
                break

            if status == 409:
                write_event("Zotero reports the library is locked/busy (HTTP 409).")
                if attempt < max_attempts:
                    delay = 2 ** attempt
                    write_event("Waiting {}s before retrying {}.".format(delay, label))
                    time.sleep(delay)
                    continue
                failures.append((label, "HTTP 409 library locked"))
                break

            if status == 412:
                # Reusing the same write token after a lost response can produce 412
                # because Zotero already accepted the first request. Verify first.
                write_event("Zotero returned HTTP 412; checking whether the earlier write already succeeded.")
                try:
                    found = _find_payload_in_zotero(item)
                except Exception as verify_exc:
                    found = None
                    write_event("Verification check failed: {}".format(verify_exc))
                if found:
                    write_event("{} is present in Zotero; no duplicate retry needed.".format(label))
                    created += 1
                    done = True
                    break
                token = uuid.uuid4().hex
                if attempt < max_attempts:
                    continue
                failures.append((label, "HTTP 412 and item not found after verification"))
                break

            if status >= 500:
                write_event("Zotero returned a server error for {}: HTTP {} {}".format(label, status, obj))
                if attempt < max_attempts:
                    delay = 2 ** attempt
                    time.sleep(delay)
                    continue
                failures.append((label, "HTTP {}: {}".format(status, obj)))
                break

            if status >= 400:
                failures.append((label, "HTTP {}: {}".format(status, obj)))
                write_event("Zotero rejected {}: HTTP {} {}".format(label, status, obj))
                break

            success = {}
            failed = {}
            if isinstance(obj, dict):
                success = obj.get("success") or obj.get("successful") or {}
                failed = obj.get("failed") or {}
            if failed:
                failures.append((label, "Zotero item failure: {}".format(failed)))
                write_event("Zotero rejected {}: {}".format(label, failed))
                break
            if success:
                created += 1
                write_event("Added {} to Zotero.".format(label))
                done = True
                break

            # A successful HTTP response without a recognizable body is ambiguous.
            write_event("Zotero returned HTTP {} but no recognizable item result; verifying library state.".format(status))
            try:
                found = _find_payload_in_zotero(item)
            except Exception as verify_exc:
                found = None
                write_event("Verification check failed: {}".format(verify_exc))
            if found:
                created += 1
                write_event("{} is present in Zotero.".format(label))
                done = True
                break
            if attempt < max_attempts:
                delay = 2 ** attempt
                time.sleep(delay)
                continue
            failures.append((label, "HTTP {} without success result, item not found".format(status)))
            break

        if not done and failures and failures[-1][0] == label:
            write_event("Giving up automatic import for {} after {} attempt(s); the rest of the document will continue.".format(label, max_attempts))

    return created, failures

def zotero_get(path):
    req = urllib.request.Request(
        ZOTERO_API + path,
        headers={"User-Agent": "PMID-DOCX-to-Zotero/1.0", "Zotero-API-Version": "3", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8")), dict(response.headers.items())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            sys.exit("Zotero returned 403. Enable Settings -> Advanced -> Allow other applications on this computer to communicate with Zotero.")
        raise
    except urllib.error.URLError as e:
        sys.exit(f"Could not connect to Zotero at localhost:23119. Make sure Zotero is open.\n{e}")


def load_all_zotero_items():
    items = []
    start = 0
    limit = 100
    while True:
        batch, headers = zotero_get(f"/users/0/items/top?format=json&limit={limit}&start={start}")
        if not batch:
            break
        items.extend(batch)
        start += len(batch)
        total = headers.get("Total-Results") or headers.get("total-results")
        if total:
            try:
                if start >= int(total):
                    break
            except ValueError:
                pass
        if len(batch) < limit:
            break
    return items


def load_all_zotero_collections(prefix="/users/0"):
    """Return all collections for one Zotero library prefix.

    The local API is not paginated by default, so one request is sufficient.
    We keep the prefix explicit because collection keys are only valid inside
    the library they belong to.
    """
    obj, _headers, status, _elapsed = zotero_raw_request(
        "GET", prefix + "/collections?format=json", timeout=20
    )
    if status >= 400:
        raise RuntimeError("Could not read Zotero collections from {} (HTTP {}): {}".format(prefix, status, obj))
    if not isinstance(obj, list):
        raise RuntimeError("Unexpected Zotero collections response from {}: {}".format(prefix, obj))
    return obj


def _collection_entries(prefix="/users/0"):
    entries = {}
    for obj in load_all_zotero_collections(prefix):
        data = obj.get("data") or {}
        key = data.get("key") or obj.get("key")
        name = str(data.get("name") or "").strip()
        parent = data.get("parentCollection") or None
        if key and name:
            entries[key] = {"key": key, "name": name, "parent": parent, "prefix": prefix}
    return entries


def _collection_paths_for_prefix(prefix="/users/0"):
    entries = _collection_entries(prefix)
    cache = {}

    def path_for(key, trail=None):
        if key in cache:
            return cache[key]
        if key not in entries:
            return ""
        trail = set(trail or ())
        if key in trail:
            return entries[key]["name"]
        trail.add(key)
        parent = entries[key]["parent"]
        if parent and parent in entries:
            parent_path = path_for(parent, trail)
            path = (parent_path + " / " if parent_path else "") + entries[key]["name"]
        else:
            path = entries[key]["name"]
        cache[key] = path
        return path

    out = []
    for key in entries:
        out.append((path_for(key), key))
    out.sort(key=lambda x: x[0].casefold())
    return out


def zotero_collection_paths():
    """Personal-library collection paths only.

    References resolved by this tool live in My Library, so their collection
    membership must also point to a My Library collection key. Group-library
    collections are a different library and cannot be used as membership keys
    on My Library items.
    """
    return _collection_paths_for_prefix("/users/0")


def _get_collection_data(collection_key, prefix="/users/0"):
    obj, _headers, status, _elapsed = zotero_raw_request(
        "GET", prefix + "/collections/{}?format=json".format(collection_key), timeout=20
    )
    if status == 404:
        return None
    if status >= 400:
        raise RuntimeError(
            "Could not read Zotero collection {} from {} (HTTP {}): {}".format(
                collection_key, prefix, status, obj
            )
        )
    if isinstance(obj, dict):
        data = obj.get("data")
        if isinstance(data, dict):
            return data
    return None


def _group_collection_hits(query):
    """Diagnostic only: find same-named collections in group libraries."""
    hits = []
    try:
        groups, _headers, status, _elapsed = zotero_raw_request(
            "GET", "/users/0/groups?format=json", timeout=20
        )
    except Exception:
        return hits
    if status >= 400 or not isinstance(groups, list):
        return hits

    q = (query or "").casefold()
    for g in groups:
        data = g.get("data") if isinstance(g, dict) else None
        data = data if isinstance(data, dict) else (g if isinstance(g, dict) else {})
        gid = data.get("id") or data.get("groupID") or g.get("id") or g.get("groupID")
        name = data.get("name") or g.get("name") or "Group {}".format(gid)
        if gid is None:
            continue
        prefix = "/groups/{}".format(gid)
        try:
            paths = _collection_paths_for_prefix(prefix)
        except Exception:
            continue
        for path, key in paths:
            if not q or q in path.casefold():
                hits.append((str(name), path, key, prefix))
    return hits


def _extract_created_key(obj):
    if not isinstance(obj, dict):
        return None
    success = obj.get("successful") or obj.get("success") or {}
    saved = success.get("0") if isinstance(success, dict) else None
    if isinstance(saved, str):
        return saved
    if isinstance(saved, dict):
        return saved.get("key") or (saved.get("data") or {}).get("key")
    return None


def create_zotero_collection(name, parent_key=None):
    """Create one collection in My Library and return (key, data)."""
    name = str(name or "").strip()
    if not name:
        raise RuntimeError("Collection name cannot be blank.")

    # Never create a duplicate exact-name top-level collection silently.
    for path, key in zotero_collection_paths():
        if path.casefold() == name.casefold():
            data = _get_collection_data(key)
            if data:
                return key, data

    server_id = zotero_server_id()
    if not server_id:
        raise RuntimeError("This Zotero version does not support local API writes.")
    api_key = zotero_write_key(server_id)
    payload = [{"name": name, "parentCollection": parent_key or False}]
    token = uuid.uuid4().hex

    for attempt in range(2):
        obj, _headers, status, _elapsed = zotero_raw_request(
            "POST",
            "/users/0/collections",
            payload=payload,
            api_key=api_key,
            server_id=server_id,
            extra_headers={"Zotero-Write-Token": token},
            timeout=60,
        )
        if status == 401 and attempt == 0:
            clear_cached_write_key()
            api_key = authorize_zotero_write(server_id)
            continue
        if status == 409:
            raise RuntimeError("Zotero says My Library is locked/busy while creating the collection (HTTP 409).")
        if status >= 400:
            raise RuntimeError("Zotero could not create collection {!r} (HTTP {}): {}".format(name, status, obj))
        key = _extract_created_key(obj)
        if not key:
            raise RuntimeError("Zotero returned success but no collection key: {}".format(obj))
        data = _get_collection_data(key)
        if not data:
            raise RuntimeError("Zotero created collection key {} but it could not be read back.".format(key))
        return key, data
    raise RuntimeError("Could not authorize collection creation.")


def resolve_requested_collection(name):
    """Resolve a GUI/CLI-supplied collection name without interactive prompts.

    Resolution order:
      1. exact full-path match (case-insensitive)
      2. unique exact leaf-name match
      3. unique substring match
      4. no match -> create a new top-level My Library collection

    Ambiguous matches raise CollectionSelectionError so the caller can ask the
    user to type a more specific/full path rather than silently choosing one.
    """
    requested = str(name or "").strip()
    if not requested:
        return None, None

    try:
        paths = zotero_collection_paths()
    except Exception as exc:
        raise CollectionSelectionError(
            "Could not read My Library collections from Zotero: {}".format(exc)
        )

    needle = requested.casefold()
    exact_path = [(path, key) for path, key in paths if path.casefold() == needle]
    if len(exact_path) == 1:
        matches = exact_path
    else:
        exact_leaf = [
            (path, key) for path, key in paths
            if path.rsplit(" / ", 1)[-1].casefold() == needle
        ]
        if len(exact_leaf) == 1:
            matches = exact_leaf
        elif len(exact_leaf) > 1:
            matches = exact_leaf
        else:
            partial = [(path, key) for path, key in paths if needle in path.casefold()]
            matches = partial

    if len(matches) == 1:
        path, key = matches[0]
        data = _get_collection_data(key, "/users/0")
        if not data:
            raise CollectionSelectionError(
                "Zotero no longer reports collection {!r} [key {}]. Try again.".format(path, key)
            )
        print("Using collection: {}".format(path))
        return key, path

    if len(matches) > 1:
        preview = "\n".join("  - {}".format(path) for path, _key in matches[:12])
        if len(matches) > 12:
            preview += "\n  ... and {} more".format(len(matches) - 12)
        raise CollectionSelectionError(
            "Collection name/path {!r} matches more than one Zotero collection. "
            "Use a more specific name or the full collection path.\n{}".format(requested, preview)
        )

    # No match: this inline GUI field doubles as the new-collection name.
    # Do not interpret a path-looking value as nested structure; creating nested
    # collections requires an explicit parent key. Keep creation predictable.
    if " / " in requested:
        raise CollectionSelectionError(
            "No existing collection matched {!r}. Because that looks like a collection path, "
            "it was not created automatically. Use an existing full path or enter a simple name "
            "for a new top-level collection.".format(requested)
        )
    try:
        key, data = create_zotero_collection(requested)
    except Exception as exc:
        raise CollectionSelectionError(
            "No existing collection matched {!r}, and Zotero could not create it: {}".format(requested, exc)
        )
    display = str(data.get("name") or requested)
    print("Created and using collection: {}".format(display))
    return key, display


def choose_import_collection():
    """Return (collection_key, display_path), or (None, None) for My Library only."""
    print("\nDo you want to put references in a specific collection/folder?")
    print("[Enter] No (they go into My Library)")
    print("[E] Yes, to an existing collection")
    print("[N] Yes, to a new collection")
    choice = input("Choice: ").strip().lower()
    if not choice:
        return None, None

    if choice in ("n", "new", "create"):
        name = input("New collection name: ").strip()
        if not name:
            print("No collection created.")
            return None, None
        try:
            key, data = create_zotero_collection(name)
        except Exception as exc:
            print("Could not create Zotero collection: {}".format(exc))
            return None, None
        print("Using My Library collection: {} [key {}]".format(data.get("name") or name, key))
        return key, str(data.get("name") or name)

    if choice not in ("e", "existing", "c", "collection", "folder"):
        print("Unrecognized collection choice; leaving membership unchanged.")
        return None, None

    try:
        paths = zotero_collection_paths()
    except Exception as exc:
        print("Could not read My Library collections: {}".format(exc))
        return None, None
    if not paths:
        print("No collections exist in My Library.")
        return None, None

    while True:
        query_raw = input("Type part of the existing collection name/path (blank = show all, / = cancel): ").strip()
        if query_raw == "/":
            return None, None
        query = query_raw.casefold()
        matches = [(path, key) for path, key in paths if not query or query in path.casefold()]
        if not matches:
            print("No matching collection was returned by Zotero in My Library.")
            group_hits = _group_collection_hits(query_raw)
            if group_hits:
                print("However, Zotero reports matching collection(s) in GROUP libraries:")
                for group_name, path, key, _prefix in group_hits[:20]:
                    print("  Group {!r}: {} [key {}]".format(group_name, path, key))
                print("A My Library item cannot use a group-library collection key. Group-library copying is a separate operation.")
            print("Try another search, or choose / to cancel.")
            continue
        if len(matches) > 60:
            print("{} matches; type a more specific search.".format(len(matches)))
            continue

        # A unique search result is unambiguous: validate it and use it immediately.
        if len(matches) == 1:
            path, key = matches[0]
            data = _get_collection_data(key, "/users/0")
            if not data:
                print("Zotero no longer reports collection key {} in My Library. Refreshing the collection list.".format(key))
                paths = zotero_collection_paths()
                continue
            actual_name = str(data.get("name") or "").strip()
            print("Using existing collection: {}".format(path))
            if actual_name and actual_name not in path:
                print("Zotero reports collection name: {}".format(actual_name))
            return key, path

        for i, (path, key) in enumerate(matches, 1):
            print("  {:>2}. {}  [key {}]".format(i, path, key))
        raw = input("Choose collection number, or Enter to cancel: ").strip()
        if not raw:
            return None, None
        try:
            number = int(raw)
        except ValueError:
            print("Enter a number from the list.")
            continue
        if 1 <= number <= len(matches):
            path, key = matches[number - 1]
            # Validate the exact key immediately before any writes.
            data = _get_collection_data(key, "/users/0")
            if not data:
                print("Zotero no longer reports collection key {} in My Library. Refreshing the collection list.".format(key))
                paths = zotero_collection_paths()
                continue
            actual_name = str(data.get("name") or "").strip()
            print("Validated My Library collection: {} [key {}]".format(path, key))
            if actual_name and actual_name not in path:
                print("Zotero reports collection name: {}".format(actual_name))
            return key, path
        print("Enter a number from the list.")


def _get_zotero_item_data(item_key):
    obj, _headers, status, _elapsed = zotero_raw_request(
        "GET", "/users/0/items/{}?format=json".format(item_key), timeout=20
    )
    if status >= 400:
        raise RuntimeError("Could not read Zotero item {} (HTTP {}): {}".format(item_key, status, obj))
    if isinstance(obj, dict):
        data = obj.get("data")
        if isinstance(data, dict):
            return data
    return {}


def ensure_refs_in_collection(refs, collection_key, collection_path, max_attempts=3, timeout=60):
    """Ensure every supplied My Library item is a member of one validated My Library collection."""
    if not collection_key:
        return {"total": 0, "already": 0, "added": 0, "failed": []}

    # Zotero collection keys are library-scoped. Validate the exact selected key
    # in My Library before touching any item.
    selected = _get_collection_data(collection_key, "/users/0")
    if not selected:
        raise RuntimeError(
            "Selected collection {!r} [key {}] is not present in My Library according to Zotero's /users/0/collections endpoint.".format(
                collection_path, collection_key
            )
        )

    unique = {}
    for ref in refs:
        if ref is not None and getattr(ref, "key", None):
            unique[ref.key] = ref
    refs = list(unique.values())
    stats = {"total": len(refs), "already": 0, "added": 0, "failed": []}
    if not refs:
        return stats

    print("\nEnsuring {} resolved document reference(s) are in My Library collection: {} [key {}]".format(
        len(refs), collection_path, collection_key
    ))
    server_id = zotero_server_id()
    if not server_id:
        raise RuntimeError("This Zotero version does not support local API writes.")
    api_key = zotero_write_key(server_id)

    for ref in refs:
        done = False
        for attempt in range(1, max_attempts + 1):
            try:
                data = _get_zotero_item_data(ref.key)
            except Exception as exc:
                if attempt < max_attempts:
                    time.sleep(2 ** attempt)
                    continue
                stats["failed"].append((ref.key, "could not read item: {}".format(exc)))
                break

            collections = list(data.get("collections") or [])
            if collection_key in collections:
                stats["already"] += 1
                done = True
                break

            version = data.get("version")
            if version is None:
                stats["failed"].append((ref.key, "item has no Zotero version; cannot make a safe PATCH"))
                break

            # Official Zotero PATCH semantics: arrays are complete lists. Preserve
            # every existing collection key and append the selected one. Use the
            # item's current version as If-Unmodified-Since-Version.
            payload = {"collections": collections + [collection_key]}
            headers = {"If-Unmodified-Since-Version": str(version)}
            write_event("Adding Zotero item {} to collection {} [key {}] (attempt {}/{})...".format(
                ref.key, collection_path, collection_key, attempt, max_attempts
            ))
            try:
                obj, response_headers, status, elapsed = zotero_raw_request(
                    "PATCH",
                    "/users/0/items/{}".format(ref.key),
                    payload=payload,
                    api_key=api_key,
                    server_id=server_id,
                    extra_headers=headers,
                    timeout=timeout,
                )
            except RuntimeError as exc:
                write_event(str(exc))
                # Timeout is ambiguous: verify membership before retrying.
                try:
                    verify = _get_zotero_item_data(ref.key)
                    if collection_key in (verify.get("collections") or []):
                        stats["added"] += 1
                        write_event("Collection membership verified after timeout for {}.".format(ref.key))
                        done = True
                        break
                except Exception:
                    pass
                if attempt < max_attempts:
                    time.sleep(2 ** attempt)
                    continue
                stats["failed"].append((ref.key, str(exc)))
                break

            write_event("Zotero answered HTTP {} in {:.2f}s while adding {} to collection.".format(status, elapsed, ref.key))
            if status == 401:
                clear_cached_write_key()
                api_key = authorize_zotero_write(server_id)
                if attempt < max_attempts:
                    continue
            elif status == 204:
                verify = _get_zotero_item_data(ref.key)
                if collection_key in (verify.get("collections") or []):
                    stats["added"] += 1
                    done = True
                    break
                write_event("Zotero returned 204, but item {} did not contain collection key {} when read back.".format(ref.key, collection_key))
            elif status == 412 and attempt < max_attempts:
                # Item changed since GET. Re-read fresh version and try again.
                write_event("Item {} changed during update (HTTP 412); re-reading current version.".format(ref.key))
                time.sleep(1)
                continue
            elif status == 409 and attempt < max_attempts:
                write_event("Zotero reports My Library is locked/busy (HTTP 409).")
                time.sleep(2 ** attempt)
                continue
            else:
                stats["failed"].append((
                    ref.key,
                    "HTTP {} while adding collection {!r} [key {}]: {}".format(status, collection_path, collection_key, obj),
                ))
                break

        if not done and not any(k == ref.key for k, _ in stats["failed"]):
            stats["failed"].append((ref.key, "collection membership could not be verified"))

    print("Collection result for this document: {} already there, {} added, {} failed.".format(
        stats["already"], stats["added"], len(stats["failed"])
    ))
    if stats["failed"]:
        for key, reason in stats["failed"]:
            print("  Could not add {}: {}".format(key, reason))
    return stats


def item_label(data):
    creators = [c for c in (data.get("creators") or []) if c.get("creatorType") in ("author", "bookAuthor", "editor", "contributor")]
    names = []
    for c in creators:
        name = (c.get("lastName") or c.get("name") or c.get("firstName") or "").strip()
        if name:
            names.append(name)
    if not names:
        author = "Unknown author"
    elif len(names) == 1:
        author = names[0]
    elif len(names) == 2:
        author = "{} and {}".format(names[0], names[1])
    else:
        author = "{} et al.".format(names[0])
    m = re.search(r"\b(?:18|19|20)\d{2}\b", str(data.get("date") or ""))
    year = m.group(0) if m else "n.d."
    return "{} ({})".format(author, year)


def item_pmids(data):
    out = set()
    for field in ("extra", "archiveLocation", "url"):
        value = str(data.get(field) or "")
        out.update(m.group(1) for m in PMID_LABELED_RE.finditer(value))
        out.update(m.group(1) for m in PUBMED_URL_RE.finditer(value))
    if "pubmed" in str(data.get("archive") or "").lower():
        m = PMID_RE.search(str(data.get("archiveLocation") or ""))
        if m:
            out.add(m.group(1))
    return out


def item_dois(data):
    out = set()
    direct = str(data.get("DOI") or "").strip()
    if direct:
        out.add(normalize_doi(direct))
    for field in ("extra", "url"):
        value = str(data.get(field) or "")
        out.update(normalize_doi(m.group(1)) for m in DOI_RE.finditer(value))
    return set(d for d in out if d)


def normalize_title(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = unicodedata.normalize("NFKC", value).casefold()
    value = "".join(ch if ch.isalnum() else " " for ch in value)
    return re.sub(r"\s+", " ", value).strip()


def year_from_data(data):
    m = re.search(r"\b(?:18|19|20)\d{2}\b", str(data.get("date") or ""))
    return m.group(0) if m else ""


def first_author_from_data(data):
    for c in data.get("creators") or []:
        if c.get("creatorType") == "author":
            return (c.get("lastName") or c.get("name") or "").strip().casefold()
    return ""


def zotero_item_uri(item, key):
    library = item.get("library") or {}
    library_type = str(library.get("type") or "")
    library_id = library.get("id")
    if library_type == "user" and library_id not in (None, "", 0, "0"):
        return "http://zotero.org/users/{}/items/{}".format(library_id, key)
    if library_type == "group" and library_id not in (None, ""):
        return "http://zotero.org/groups/{}/items/{}".format(library_id, key)

    # Local API item links normally contain the real numeric user/group id.
    links = item.get("links") or {}
    for link_name in ("self", "alternate"):
        link = links.get(link_name) or {}
        href = str(link.get("href") or "")
        m = re.search(r"/(users|groups)/(\d+)/items/([A-Z0-9]+)", href, re.I)
        if m:
            kind, lib_id, item_key = m.groups()
            base = "users" if kind.lower() == "users" else "groups"
            return "http://zotero.org/{}/{}/items/{}".format(base, lib_id, item_key)

    # Current ODF/DOCX Scan accepts zotero://select as a fallback URI, but the
    # numeric canonical URI above is preferred for a true Word citation field.
    return "zotero://select/library/items/{}".format(key)


def build_ref(item):
    data = item.get("data") or {}
    if data.get("itemType") in ("attachment", "note", "annotation"):
        return None
    key = data.get("key") or item.get("key")
    if not key:
        return None
    return ZoteroRef(
        key,
        item_label(data),
        str(data.get("title") or ""),
        zotero_item_uri(item, key),
        data=data,
        item=item,
    )


def load_zotero_index():
    pmid_map = {}
    doi_map = {}
    title_map = {}
    refs = []
    for item in load_all_zotero_items():
        ref = build_ref(item)
        if ref is None:
            continue
        refs.append(ref)
        for pmid in item_pmids(ref.data):
            pmid_map.setdefault(pmid, ref)
        for doi in item_dois(ref.data):
            doi_map.setdefault(doi, ref)
        nt = normalize_title(ref.title)
        if nt:
            title_map.setdefault(nt, []).append(ref)
    return pmid_map, doi_map, title_map, refs


def article_title(article):
    med = article.find("MedlineCitation") if article is not None else None
    art = med.find("Article") if med is not None else None
    return clean_ris_text(node_text(art.find("ArticleTitle"))) if art is not None else ""


def article_year(article):
    med = article.find("MedlineCitation") if article is not None else None
    art = med.find("Article") if med is not None else None
    if art is None:
        return ""
    journal = art.find("Journal")
    issue = journal.find("JournalIssue") if journal is not None else None
    pd = issue.find("PubDate") if issue is not None else None
    if pd is None:
        return ""
    year = node_text(pd.find("Year"))
    if year:
        return year
    m = re.search(r"\b(?:18|19|20)\d{2}\b", node_text(pd.find("MedlineDate")))
    return m.group(0) if m else ""


def article_first_author(article):
    med = article.find("MedlineCitation") if article is not None else None
    art = med.find("Article") if med is not None else None
    if art is None:
        return ""
    au = art.find("AuthorList/Author")
    if au is None:
        return ""
    return (node_text(au.find("LastName")) or node_text(au.find("CollectiveName"))).strip().casefold()


def choose_title_match(title, title_map, expected_year="", expected_author=""):
    nt = normalize_title(title)
    if not nt:
        return None
    exact = title_map.get(nt) or []
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        filtered = exact
        if expected_year:
            y = [r for r in filtered if year_from_data(r.data) == expected_year]
            if y:
                filtered = y
        if expected_author:
            a = [r for r in filtered if first_author_from_data(r.data) == expected_author.casefold()]
            if a:
                filtered = a
        if len(filtered) == 1:
            return filtered[0]
        return None

    # Fuzzy title matching is deliberately conservative and requires supporting
    # author or year agreement to avoid merging distinct publications.
    best = None
    best_score = 0.0
    for candidate_title, candidate_refs in title_map.items():
        score = difflib.SequenceMatcher(None, nt, candidate_title).ratio()
        if score < 0.97 or score <= best_score:
            continue
        for ref in candidate_refs:
            year_ok = bool(expected_year and year_from_data(ref.data) == expected_year)
            author_ok = bool(expected_author and first_author_from_data(ref.data) == expected_author.casefold())
            if year_ok or author_ok:
                best = ref
                best_score = score
    return best


def identifiers_in_text(text):
    found = []
    mask = list(text)
    for m in DOI_RE.finditer(text):
        raw_doi = m.group(1)
        trimmed = _trim_doi_value(raw_doi)
        doi = normalize_doi(trimmed)
        if doi:
            # Include an optional doi:/https://doi.org prefix, but do not swallow
            # ordinary sentence punctuation or an outer citation parenthesis.
            effective_end = m.start(1) + len(trimmed)
            found.append((m.start(), effective_end, "doi", doi))
            for i in range(m.start(), effective_end):
                mask[i] = " "
    masked = "".join(mask)
    for m in PMID_RE.finditer(masked):
        found.append((m.start(), m.end(), "pmid", m.group(1)))
    return sorted(found, key=lambda x: x[0])


def balanced_outer_spans(text):
    stack = []
    spans = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for i, ch in enumerate(text):
        if ch in "([{":
            stack.append((ch, i))
        elif ch in ")]}":
            if stack and stack[-1][0] == pairs[ch]:
                _, start = stack.pop()
                if not stack:
                    spans.append((start, i + 1))
    return spans


def pmid_wrapper_spans(text):
    """Return tolerant bracketed PMID groups, including mismatched bracket pairs."""
    label = r"(?:PMIDs?|PubMed(?:\s+IDs?)?)"
    pattern = re.compile(
        r"(?P<open>[\(\[\{])\s*"
        r"(?P<body>(?:" + label + r"\s*:?\s*)?\d{6,8}"
        r"(?:\s*(?:[,;]\s*|\s+)(?:" + label + r"\s*:?\s*)?\d{6,8})*)"
        r"\s*(?P<close>[\)\]\}])",
        re.I,
    )
    return [(m.start(), m.end(), m.group("body")) for m in pattern.finditer(text)]

def strip_identifiers(text):
    text = DOI_RE.sub(" ", text)
    text = re.sub(r"\b(?:PMIDs?|PubMed(?:\s+IDs?)?)\s*:?\s*", " ", text, flags=re.I)
    text = PMID_RE.sub(" ", text)
    return re.sub(r"[\s,;]+", "", text)


def generate_citation_id():
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(8))


def provisional_citation(refs):
    labels = []
    seen = set()
    for ref in refs:
        if ref.key not in seen:
            seen.add(ref.key)
            m = re.match(r"^(.*?) \(([^()]*)\)$", ref.label)
            labels.append("{} {}".format(m.group(1), m.group(2)) if m else ref.label)
    return "(" + "; ".join(labels) + ")"


def build_citation_data(refs):
    citation_items = []
    seen = set()
    for ref in refs:
        if ref.key in seen:
            continue
        seen.add(ref.key)
        citation_items.append({"uris": [ref.uri]})
    display = provisional_citation(refs)
    return {
        "citationID": generate_citation_id(),
        "properties": {
            "formattedCitation": display,
            "plainCitation": display,
        },
        "citationItems": citation_items,
        "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json",
    }, display


def escape_xml_text(value):
    return xml_escape(value, {'"': '&quot;', "'": '&apos;'})


def _run_properties_for_text_match(xml, match):
    """Return the direct Word run formatting that owns one <w:t> match."""
    prefix = xml[:match.start()]
    run_starts = list(re.finditer(r'<w:r\b[^>]*>', prefix))
    if not run_starts:
        return ""
    run_start = run_starts[-1].start()
    if prefix.rfind("</w:r>") > run_start:
        return ""
    run_prefix = xml[run_start:match.start()]
    prop_match = re.search(
        r'(<w:rPr\b[^>]*/>|<w:rPr\b[^>]*>[\s\S]*?</w:rPr>)',
        run_prefix,
    )
    return prop_match.group(1) if prop_match else ""


def _styled_word_run(inner_xml, run_properties=""):
    return '<w:r>{}{}</w:r>'.format(run_properties or "", inner_xml)


def build_word_field_xml(refs, run_properties=""):
    citation_data, display = build_citation_data(refs)
    json_text = json.dumps(citation_data, ensure_ascii=False, separators=(",", ":"))
    escaped_json = escape_xml_text(json_text)
    escaped_display = escape_xml_text(display)
    return (
        _styled_word_run('<w:fldChar w:fldCharType="begin"/>', run_properties)
        + _styled_word_run(
            '<w:instrText xml:space="preserve"> ADDIN ZOTERO_ITEM CSL_CITATION {} </w:instrText>'.format(escaped_json),
            run_properties,
        )
        + _styled_word_run('<w:fldChar w:fldCharType="separate"/>', run_properties)
        + _styled_word_run('<w:t>{}</w:t>'.format(escaped_display), run_properties)
        + _styled_word_run('<w:fldChar w:fldCharType="end"/>', run_properties)
    )


def build_inline_field_insertion(refs, run_properties=""):
    # Close the current Word text run, insert the Zotero field, then reopen
    # ordinary text with the same formatting so insertion does not reset it.
    return (
        '</w:t></w:r>'
        + build_word_field_xml(refs, run_properties)
        + '<w:r>{}<w:t xml:space="preserve">'.format(run_properties or "")
    )


def find_replacements(text, pmid_map, doi_map):
    replacements = []
    missing_pmids = set()
    missing_dois = set()
    manual = []
    protected = []

    def ref_for(kind, value):
        return pmid_map.get(value) if kind == "pmid" else doi_map.get(normalize_doi(value))

    def overlaps(s, e):
        return any(s < r.end and e > r.start for r in replacements)

    # Tolerant PMID-only wrappers first. These intentionally accept any
    # standard bracket family and mismatched pairs such as (12345678].
    # The wrapper is placeholder punctuation, so consume it with the PMID(s).
    for start, end, body in pmid_wrapper_spans(text):
        ids = identifiers_in_text(body)
        if not ids:
            continue
        refs = []
        missing = []
        for _, _, kind, value in ids:
            ref = ref_for(kind, value)
            if ref:
                refs.append(ref)
            else:
                missing.append((kind, value))
        if missing:
            for kind, value in missing:
                (missing_pmids if kind == "pmid" else missing_dois).add(value)
            protected.append((start, end))
            continue
        replacements.append(Replacement(start, end, refs, [v for _, _, _, v in ids]))

    # Properly balanced groups can also contain DOI references or mixtures of
    # DOI and PMID references. They become one Zotero citation field.
    for start, end in balanced_outer_spans(text):
        if overlaps(start, end):
            continue
        chunk = text[start:end]
        ids = identifiers_in_text(chunk)
        if not ids:
            continue
        refs = []
        missing = []
        for _, _, kind, value in ids:
            ref = ref_for(kind, value)
            if ref:
                refs.append(ref)
            else:
                missing.append((kind, value))
        if missing:
            for kind, value in missing:
                (missing_pmids if kind == "pmid" else missing_dois).add(value)
            protected.append((start, end))
            continue

        residual = strip_identifiers(chunk[1:-1])
        normalized = re.sub(r"[^a-z]", "", residual.lower())
        if not residual:
            replacements.append(Replacement(start, end, refs, [v for _, _, _, v in ids]))
        elif normalized == "addmore":
            replacements.append(Replacement(start, end, refs, [v for _, _, _, v in ids], " [add more]"))
        else:
            protected.append((start, end))
            manual.append(chunk)
    def inside_protected(pos):
        return any(s <= pos < e for s, e in protected)

    # Explicit PMID/PubMed labels outside brackets. A colon is optional and
    # common plural forms are accepted.
    cluster_re = re.compile(
        r"\b(?:PMIDs?|PubMed(?:\s+IDs?)?)\s*:?\s*"
        r"(?P<body>\d{6,8}(?:\s*(?:[,;]\s*|\s+)\d{6,8})*)",
        re.I,
    )
    for m in cluster_re.finditer(text):
        if overlaps(m.start(), m.end()) or inside_protected(m.start()):
            continue
        pmids = PMID_RE.findall(m.group("body"))
        refs = [pmid_map.get(p) for p in pmids]
        if any(r is None for r in refs):
            missing_pmids.update(p for p, r in zip(pmids, refs) if r is None)
            protected.append((m.start(), m.end()))
            continue
        replacements.append(Replacement(m.start(), m.end(), refs, pmids))

    # Bare comma/semicolon PMID lists are also a single citation, even when
    # they are not wrapped in brackets.
    bare_cluster_re = re.compile(
        r"(?<!\d)(?P<body>\d{6,8}(?:\s*[,;]\s*\d{6,8})+)(?!\d)"
    )
    for m in bare_cluster_re.finditer(text):
        if overlaps(m.start(), m.end()) or inside_protected(m.start()):
            continue
        pmids = PMID_RE.findall(m.group("body"))
        refs = [pmid_map.get(p) for p in pmids]
        if any(r is None for r in refs):
            missing_pmids.update(p for p, r in zip(pmids, refs) if r is None)
            protected.append((m.start(), m.end()))
            continue
        replacements.append(Replacement(m.start(), m.end(), refs, pmids))
    # Remaining standalone PMIDs and DOI strings.
    for start, end, kind, value in identifiers_in_text(text):
        if overlaps(start, end) or inside_protected(start):
            continue
        ref = ref_for(kind, value)
        if ref:
            replacements.append(Replacement(start, end, [ref], [value]))
        else:
            (missing_pmids if kind == "pmid" else missing_dois).add(value)

    replacements.sort(key=lambda r: r.start)
    return replacements, missing_pmids, missing_dois, manual


def patch_paragraph(paragraph_xml, replacements):
    matches = list(WT_RE.finditer(paragraph_xml))
    if not matches or not replacements:
        return paragraph_xml
    texts = [html.unescape(m.group(2)) for m in matches]
    run_properties = [_run_properties_for_text_match(paragraph_xml, m) for m in matches]
    intervals = []
    cursor = 0
    for i, text in enumerate(texts):
        intervals.append((i, cursor, cursor + len(text)))
        cursor += len(text)

    token_fields = {}
    # Work right-to-left so offsets calculated from the original text remain valid.
    for repl in reversed(replacements):
        affected = [(i, s, e) for i, s, e in intervals if repl.start < e and repl.end > s]
        if not affected:
            continue
        fi, fs, _ = affected[0]
        li, ls, _ = affected[-1]

        # Prefer the text immediately before the citation. At the start of a
        # paragraph, fall back to the run that contained the identifier itself.
        style_index = fi
        if repl.start > 0:
            prior_pos = repl.start - 1
            for idx, start, end in intervals:
                if start <= prior_pos < end:
                    style_index = idx
                    break
        style_xml = run_properties[style_index] if style_index < len(run_properties) else ""

        token = "ZOTEROFIELD_{}_{}".format(generate_citation_id(), len(token_fields))
        token_fields[token] = build_inline_field_insertion(repl.refs, style_xml)
        local_start = max(0, repl.start - fs)
        if fi == li:
            local_end = max(0, repl.end - fs)
            texts[fi] = texts[fi][:local_start] + token + repl.trailing_text + texts[fi][local_end:]
        else:
            texts[fi] = texts[fi][:local_start] + token + repl.trailing_text
            for idx, _, _ in affected[1:-1]:
                texts[idx] = ""
            local_end = max(0, repl.end - ls)
            texts[li] = texts[li][local_end:]

    out = paragraph_xml
    for m, new_text in reversed(list(zip(matches, texts))):
        out = out[:m.start(2)] + xml_escape(new_text) + out[m.end(2):]
    for token, field_insertion in token_fields.items():
        out = out.replace(token, field_insertion)
    # Word trims edge whitespace unless xml:space=preserve is set.
    out = re.sub(r'<w:t>([^<]*\s)</w:t>', r'<w:t xml:space="preserve">\1</w:t>', out)
    return out


_BIBLIOGRAPHY_HEADINGS = {
    "references", "bibliography", "works cited", "literature cited",
    "references cited", "reference list",
}
_FIELD_CHAR_TOKEN_RE = re.compile(
    r'<w:fldChar\b[^>]*\bw:fldCharType="(?P<kind>begin|end)"[^>]*/?>', re.I
)
_INSTR_TEXT_RE = re.compile(
    r'<w:instrText\b[^>]*>(?P<code>[\s\S]*?)</w:instrText>', re.I
)


def _field_span_containing_instruction(document_xml, instr_start, instr_end):
    stack = []
    for token in _FIELD_CHAR_TOKEN_RE.finditer(document_xml, 0, instr_start):
        if token.group("kind").lower() == "begin":
            stack.append(token.start())
        elif stack:
            stack.pop()
    if not stack:
        return None
    field_start = stack[-1]
    depth = 1
    for token in _FIELD_CHAR_TOKEN_RE.finditer(document_xml, instr_end):
        if token.group("kind").lower() == "begin":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return field_start, token.end()
    return None


def _zotero_bibliography_field_spans(document_xml):
    """Locate Zotero bibliography fields from ADDIN ZOTERO_BIBL ... CSL_BIBLIOGRAPHY."""
    spans = []
    for instr in _INSTR_TEXT_RE.finditer(document_xml):
        code = html.unescape(instr.group("code"))
        if "CSL_BIBLIOGRAPHY" not in code:
            continue
        # Zotero Word XML uses ZOTERO_BIBL; Zotero's integration layer
        # represents the same field internally as BIBL ... CSL_BIBLIOGRAPHY.
        if "ZOTERO_BIBL" not in code and not re.search(r"(?:^|\s)BIBL(?:\s|$)", code):
            continue
        span = _field_span_containing_instruction(document_xml, instr.start(), instr.end())
        if span and span not in spans:
            spans.append(span)
    return spans


def _paragraph_style_value(paragraph_xml):
    m = re.search(r'<w:pStyle\b[^>]*\bw:val="([^"]+)"[^>]*/?>', paragraph_xml, re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def _merge_spans(spans):
    if not spans:
        return []
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _bibliography_exclusion_spans(document_xml):
    """Return ranges that should never be scanned or converted as placeholders."""
    field_spans = _zotero_bibliography_field_spans(document_xml)
    paragraphs = list(PARA_RE.finditer(document_xml))
    style_spans = [
        (pm.start(), pm.end()) for pm in paragraphs
        if _paragraph_style_value(pm.group(0)).casefold() == "bibliography"
    ]
    spans = list(field_spans) + style_spans

    # Plain-text bibliographies have no Zotero field. When there is no
    # Zotero field, use the last conventional standalone heading as a
    # section boundary as well (last avoids a TOC entry).
    if not field_spans:
        heading_start = None
        for pm in paragraphs:
            heading = re.sub(r"\s+", " ", visible_text(pm.group(0))).strip()
            heading = heading.rstrip(":").strip().casefold()
            if heading in _BIBLIOGRAPHY_HEADINGS:
                heading_start = pm.start()
        if heading_start is not None:
            spans.append((heading_start, len(document_xml)))
    return _merge_spans(spans)


def _range_overlaps_spans(start, end, spans):
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _bibliography_comment_ids(document_xml, spans=None):
    spans = _bibliography_exclusion_spans(document_xml) if spans is None else spans
    out = set()
    for m in re.finditer(r'<w:commentRangeStart\b[^>]*\bw:id="(\d+)"[^>]*/>', document_xml, re.I):
        if _range_overlaps_spans(m.start(), m.end(), spans):
            out.add(int(m.group(1)))
    return out

def patch_document_xml(xml, pmid_map, doi_map):
    pieces = []
    last = 0
    stats = {"locations": 0, "ids": [], "missing_pmids": set(), "missing_dois": set(), "manual": []}
    bibliography_spans = _bibliography_exclusion_spans(xml)
    for pm in PARA_RE.finditer(xml):
        para = pm.group(0)
        pieces.append(xml[last:pm.start()])
        if (not _range_overlaps_spans(pm.start(), pm.end(), bibliography_spans)
                and "ADDIN ZOTERO_ITEM CSL_CITATION" not in para):
            text = visible_text(para)
            if PMID_RE.search(text) or DOI_RE.search(text):
                reps, mp, md, manual = find_replacements(text, pmid_map, doi_map)
                stats["missing_pmids"].update(mp)
                stats["missing_dois"].update(md)
                stats["manual"].extend(manual)
                if reps:
                    para = patch_paragraph(para, reps)
                    stats["locations"] += len(reps)
                    for r in reps:
                        stats["ids"].extend(r.identifiers)
        pieces.append(para)
        last = pm.end()
    pieces.append(xml[last:])
    return "".join(pieces), stats


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

def insert_before(xml, closing, snippet):
    pos = xml.rfind(closing)
    if pos < 0:
        raise RuntimeError(f"Could not find {closing}")
    return xml[:pos] + snippet + xml[pos:]


def hex8(signed=False):
    return f"{random.randint(0x10000000, 0x7FFFFFFE if signed else 0xFFFFFFFE):08X}"


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

def add_comment_citations_and_replies(parts, pmid_map, doi_map):
    stats = {
        "replies": 0,
        "citations_added": 0,
        "comments_updated": 0,
        "pmids": set(),
        "missing_pmids": set(),
        "missing_dois": set(),
    }
    if "word/comments.xml" not in parts:
        return parts, stats

    reply_parts = [
        "word/commentsExtended.xml",
        "word/commentsIds.xml",
        "word/commentsExtensible.xml",
    ]
    replies_supported = all(name in parts for name in reply_parts)

    comments = parts["word/comments.xml"]
    comments_ex = parts.get("word/commentsExtended.xml", "")
    comments_ids = parts.get("word/commentsIds.xml", "")
    comments_ext = parts.get("word/commentsExtensible.xml", "")
    document_xml = parts["word/document.xml"]
    bibliography_comment_ids = _bibliography_comment_ids(document_xml)

    records = comment_records(comments, comments_ex)
    max_id = max([r["id"] for r in records] or [-1])
    existing_replies = {
        r["text"].strip()
        for r in records
        if r["is_reply"] and r["text"].strip().lower().startswith("citations added")
    }

    for rec in records:
        if rec["is_reply"] or rec["id"] in bibliography_comment_ids:
            continue

        ids = identifiers_in_text(rec["text"])
        if not ids:
            continue

        refs = []
        missing = False
        for _, _, kind, value in ids:
            ref = pmid_map.get(value) if kind == "pmid" else doi_map.get(normalize_doi(value))
            if ref:
                refs.append(ref)
                if kind == "pmid":
                    stats["pmids"].add(value)
            else:
                missing = True
                (stats["missing_pmids"] if kind == "pmid" else stats["missing_dois"]).add(value)

        if missing or not refs:
            continue

        # De-duplicate references in the comment while preserving order.
        unique_refs = []
        seen = set()
        for ref in refs:
            if ref.key not in seen:
                seen.add(ref.key)
                unique_refs.append(ref)

        document_xml, added_count = append_refs_at_comment_anchor(
            document_xml, rec["id"], unique_refs
        )
        if added_count == 0:
            continue

        stats["citations_added"] += added_count
        stats["comments_updated"] += 1

        # The citation itself is the important part. A threaded reply is optional:
        # add it only when this DOCX already has the modern Word comment metadata.
        if not replies_supported:
            continue

        reply_labels = []
        for ref in unique_refs:
            label = ref.label.replace(", (", " (")
            if label not in reply_labels:
                reply_labels.append(label)
        reply_text = "Citations added: " + "; ".join(reply_labels)
        if reply_text.lower() in {x.lower() for x in existing_replies}:
            continue

        max_id += 1
        para_id = hex8()
        durable = hex8(signed=True)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        c = (
            f'<w:comment w:id="{max_id}" w:author="Zotero PMID Tool" w:date="{now}" w:initials="ZPT">'
            f'<w:p w14:paraId="{para_id}" w14:textId="77777777">'
            f'<w:pPr><w:pStyle w:val="CommentText"/></w:pPr>'
            f'<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:annotationRef/></w:r>'
            f'<w:r><w:t xml:space="preserve">{xml_escape(reply_text)}</w:t></w:r>'
            f'</w:p></w:comment>'
        )
        ce = f'<w15:commentEx w15:paraId="{para_id}" w15:paraIdParent="{rec["last_para"]}" w15:done="0"/>'
        ci = f'<w16cid:commentId w16cid:paraId="{para_id}" w16cid:durableId="{durable}"/>'
        cx = f'<w16cex:commentExtensible w16cex:durableId="{durable}" w16cex:dateUtc="{now}"/>'

        comments = insert_before(comments, "</w:comments>", c)
        comments_ex = insert_before(comments_ex, "</w15:commentsEx>", ce)
        comments_ids = insert_before(comments_ids, "</w16cid:commentsIds>", ci)
        comments_ext = insert_before(comments_ext, "</w16cex:commentsExtensible>", cx)
        existing_replies.add(reply_text)
        stats["replies"] += 1

    parts["word/document.xml"] = document_xml
    parts["word/comments.xml"] = comments
    if replies_supported:
        parts["word/commentsExtended.xml"] = comments_ex
        parts["word/commentsIds.xml"] = comments_ids
        parts["word/commentsExtensible.xml"] = comments_ext
    return parts, stats

def all_identifiers_in_docx(path):
    """Collect PMID/DOI placeholders while excluding bibliography/reference text."""
    pmids = set()
    dois = set()

    def collect_from_text(text):
        for _, _, kind, value in identifiers_in_text(text):
            if kind == "pmid":
                pmids.add(value)
            else:
                dois.add(normalize_doi(value))

    with zipfile.ZipFile(path, "r") as z:
        bibliography_comment_ids = set()
        if "word/document.xml" in z.namelist():
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
            bibliography_spans = _bibliography_exclusion_spans(xml)
            bibliography_comment_ids = _bibliography_comment_ids(xml, bibliography_spans)
            for pm in PARA_RE.finditer(xml):
                if _range_overlaps_spans(pm.start(), pm.end(), bibliography_spans):
                    continue
                collect_from_text(visible_text(pm.group(0)))

        if "word/comments.xml" in z.namelist():
            comments_xml = z.read("word/comments.xml").decode("utf-8", errors="replace")
            for cm in COMMENT_RE.finditer(comments_xml):
                if int(cm.group("id")) in bibliography_comment_ids:
                    continue
                collect_from_text(comment_visible_text(cm.group("body")))

    return pmids, dois

def docx_has_zotero_preferences(path):
    try:
        with zipfile.ZipFile(path, "r") as z:
            if "docProps/custom.xml" not in z.namelist():
                return False
            text = z.read("docProps/custom.xml").decode("utf-8", errors="replace")
            return "ZOTERO_PREF" in text
    except Exception:
        return False


def node_text(el):
    return "" if el is None else "".join(el.itertext()).strip()


def clean_ris_text(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def article_to_ris(article):
    """Convert one PubMed XML article to a Zotero-friendly RIS record."""
    med = article.find("MedlineCitation")
    pub = article.find("PubmedData")
    art = med.find("Article") if med is not None else None

    pmid = node_text(med.find("PMID")) if med is not None else ""
    title = node_text(art.find("ArticleTitle")) if art is not None else ""
    journal = ""
    journal_abbrev = ""
    year = ""
    volume = ""
    issue = ""
    pages = ""
    doi = ""

    if art is not None:
        journal_el = art.find("Journal")
        if journal_el is not None:
            journal = node_text(journal_el.find("Title"))
            ji = journal_el.find("JournalIssue")
            if ji is not None:
                volume = node_text(ji.find("Volume"))
                issue = node_text(ji.find("Issue"))
                pd = ji.find("PubDate")
                if pd is not None:
                    year = node_text(pd.find("Year"))
                    if not year:
                        medline_date = node_text(pd.find("MedlineDate"))
                        m = re.search(r"\b(?:18|19|20)\d{2}\b", medline_date)
                        year = m.group(0) if m else ""
        pages = node_text(art.find("Pagination/MedlinePgn"))

    if med is not None:
        mji = med.find("MedlineJournalInfo")
        if mji is not None:
            journal_abbrev = node_text(mji.find("MedlineTA"))

    authors = []
    if art is not None:
        for au in art.findall("AuthorList/Author"):
            group = node_text(au.find("CollectiveName"))
            if group:
                authors.append(group)
                continue
            last = node_text(au.find("LastName"))
            fore = node_text(au.find("ForeName"))
            initials = node_text(au.find("Initials"))
            if last:
                authors.append("{}, {}".format(last, fore or initials).strip(", "))

    if pub is not None:
        for aid in pub.findall("ArticleIdList/ArticleId"):
            if (aid.attrib.get("IdType") or "").lower() == "doi":
                doi = node_text(aid)
                break

    abstract_parts = []
    if art is not None:
        for ab in art.findall("Abstract/AbstractText"):
            label = ab.attrib.get("Label")
            text = node_text(ab)
            if text:
                abstract_parts.append("{}: {}".format(label, text) if label else text)

    lines = ["TY  - JOUR"]
    for author in authors:
        lines.append("AU  - {}".format(clean_ris_text(author)))
    if title:
        lines.append("TI  - {}".format(clean_ris_text(title)))
    if journal:
        lines.append("JO  - {}".format(clean_ris_text(journal)))
    if journal_abbrev:
        lines.append("J2  - {}".format(clean_ris_text(journal_abbrev)))
    if year:
        lines.append("PY  - {}".format(year))
    if volume:
        lines.append("VL  - {}".format(volume))
    if issue:
        lines.append("IS  - {}".format(issue))
    if pages:
        lines.append("SP  - {}".format(pages))
    if doi:
        lines.append("DO  - {}".format(doi))
    if pmid:
        lines.append("AN  - PMID:{}".format(pmid))
        lines.append("UR  - https://pubmed.ncbi.nlm.nih.gov/{}/".format(pmid))
    if abstract_parts:
        lines.append("AB  - {}".format(clean_ris_text(" ".join(abstract_parts))))
    lines.append("ER  -")
    return "\n".join(lines)




def pubmed_article_to_zotero_item(article):
    med = article.find("MedlineCitation")
    pub = article.find("PubmedData")
    art = med.find("Article") if med is not None else None
    pmid = node_text(med.find("PMID")) if med is not None else ""
    title = clean_ris_text(node_text(art.find("ArticleTitle"))) if art is not None else ""

    item = {
        "itemType": "journalArticle",
        "title": title,
        "creators": [],
        "tags": [],
        "collections": [],
        "relations": {},
    }

    if art is not None:
        for au in art.findall("AuthorList/Author"):
            group = clean_ris_text(node_text(au.find("CollectiveName")))
            if group:
                item["creators"].append({"creatorType": "author", "name": group})
                continue
            last = clean_ris_text(node_text(au.find("LastName")))
            first = clean_ris_text(node_text(au.find("ForeName")) or node_text(au.find("Initials")))
            if last:
                item["creators"].append({
                    "creatorType": "author",
                    "firstName": first,
                    "lastName": last,
                })

        journal_el = art.find("Journal")
        if journal_el is not None:
            journal = clean_ris_text(node_text(journal_el.find("Title")))
            if journal:
                item["publicationTitle"] = journal
            ji = journal_el.find("JournalIssue")
            if ji is not None:
                volume = node_text(ji.find("Volume"))
                issue = node_text(ji.find("Issue"))
                if volume:
                    item["volume"] = volume
                if issue:
                    item["issue"] = issue
                pd = ji.find("PubDate")
                if pd is not None:
                    year = node_text(pd.find("Year"))
                    if not year:
                        md = node_text(pd.find("MedlineDate"))
                        m = re.search(r"\b(?:18|19|20)\d{2}\b", md)
                        year = m.group(0) if m else ""
                    if year:
                        item["date"] = year
        pages = node_text(art.find("Pagination/MedlinePgn"))
        if pages:
            item["pages"] = pages

        abstract_parts = []
        for ab in art.findall("Abstract/AbstractText"):
            label = ab.attrib.get("Label")
            text = clean_ris_text(node_text(ab))
            if text:
                abstract_parts.append("{}: {}".format(label, text) if label else text)
        if abstract_parts:
            item["abstractNote"] = " ".join(abstract_parts)

    if med is not None:
        mji = med.find("MedlineJournalInfo")
        if mji is not None:
            abbrev = clean_ris_text(node_text(mji.find("MedlineTA")))
            if abbrev:
                item["journalAbbreviation"] = abbrev

    doi = ""
    if pub is not None:
        for aid in pub.findall("ArticleIdList/ArticleId"):
            if (aid.attrib.get("IdType") or "").lower() == "doi":
                doi = node_text(aid)
                break
    if doi:
        item["DOI"] = doi
    if pmid:
        item["url"] = "https://pubmed.ncbi.nlm.nih.gov/{}/".format(pmid)
        item["extra"] = "PMID: {}".format(pmid)

    return item


def fetch_crossref_message(doi):
    """Fetch metadata for one DOI from Crossref as JSON."""
    encoded = urllib.parse.quote(normalize_doi(doi), safe="")
    url = "https://api.crossref.org/works/{}".format(encoded)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PMID-DOCX-to-Zotero/3.1",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        raise RuntimeError("Crossref returned no metadata for DOI {}".format(doi))
    return message


def crossref_message_to_zotero_item(message, doi):
    """Convert basic Crossref metadata to a Zotero item payload."""
    crossref_type = str(message.get("type") or "")
    item_type = "journalArticle"
    if crossref_type == "book-chapter":
        item_type = "bookSection"
    elif crossref_type in ("book", "monograph", "reference-book"):
        item_type = "book"
    elif crossref_type in ("proceedings-article", "proceedings"):
        item_type = "conferencePaper"
    elif crossref_type in ("report", "report-series"):
        item_type = "report"

    titles = message.get("title") or []
    title = clean_ris_text(titles[0] if titles else "")
    item = {
        "itemType": item_type,
        "title": title,
        "creators": [],
        "tags": [],
        "collections": [],
        "relations": {},
        "DOI": normalize_doi(doi),
    }

    for author in message.get("author") or []:
        family = clean_ris_text(str(author.get("family") or ""))
        given = clean_ris_text(str(author.get("given") or ""))
        name = clean_ris_text(str(author.get("name") or ""))
        if family:
            item["creators"].append({
                "creatorType": "author",
                "firstName": given,
                "lastName": family,
            })
        elif name:
            item["creators"].append({"creatorType": "author", "name": name})

    containers = message.get("container-title") or []
    container = clean_ris_text(containers[0] if containers else "")
    short_containers = message.get("short-container-title") or []
    short_container = clean_ris_text(short_containers[0] if short_containers else "")

    if item_type == "journalArticle":
        if container:
            item["publicationTitle"] = container
        if short_container:
            item["journalAbbreviation"] = short_container
    elif item_type == "bookSection" and container:
        item["bookTitle"] = container
    elif item_type == "conferencePaper" and container:
        item["proceedingsTitle"] = container

    for key, zotero_key in (("volume", "volume"), ("issue", "issue"), ("page", "pages"), ("publisher", "publisher")):
        value = clean_ris_text(str(message.get(key) or ""))
        if value:
            item[zotero_key] = value

    date_value = ""
    for date_key in ("published-print", "published-online", "published", "issued"):
        date_obj = message.get(date_key) or {}
        parts = date_obj.get("date-parts") if isinstance(date_obj, dict) else None
        if parts and parts[0]:
            vals = [str(x) for x in parts[0] if x is not None]
            if vals:
                date_value = "-".join(vals)
                break
    if date_value:
        item["date"] = date_value

    url = clean_ris_text(str(message.get("URL") or ""))
    if url:
        item["url"] = url
    abstract = str(message.get("abstract") or "")
    if abstract:
        abstract = re.sub(r"<[^>]+>", " ", abstract)
        abstract = clean_ris_text(abstract)
        if abstract:
            item["abstractNote"] = abstract

    return item


def fetch_pubmed_articles(pmids, batch_size=150):
    """Fetch PubMed metadata once and return it keyed by PMID."""
    ordered = sorted(set(pmids), key=int)
    articles = {}
    for i in range(0, len(ordered), batch_size):
        batch = ordered[i:i + batch_size]
        params = urllib.parse.urlencode({
            "db": "pubmed",
            "id": ",".join(batch),
            "retmode": "xml",
        })
        req = urllib.request.Request(
            "{}?{}".format(EUTILS_FETCH, params),
            headers={"User-Agent": "PMID-DOCX-to-Zotero/3.0"},
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            root = ET.fromstring(response.read())
        for article in root.findall("PubmedArticle"):
            pmid = node_text(article.find("MedlineCitation/PMID"))
            if pmid:
                articles[pmid] = article
        if i + batch_size < len(ordered):
            time.sleep(0.35)
    missing = [p for p in ordered if p not in articles]
    return articles, missing


def article_doi(article):
    if article is None:
        return ""
    pub = article.find("PubmedData")
    if pub is not None:
        for aid in pub.findall("ArticleIdList/ArticleId"):
            if (aid.attrib.get("IdType") or "").lower() == "doi":
                return normalize_doi(node_text(aid))
    return ""


def fetch_pubmed_article_for_doi(doi):
    """Resolve a DOI through PubMed first and verify the returned DOI."""
    normalized = normalize_doi(doi)
    params = urllib.parse.urlencode({
        "db": "pubmed",
        "term": "{}[AID]".format(normalized),
        "retmode": "json",
        "retmax": "10",
    })
    req = urllib.request.Request(
        "{}?{}".format(EUTILS_SEARCH, params),
        headers={"User-Agent": "PMID-DOCX-to-Zotero/4.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    ids = ((payload.get("esearchresult") or {}).get("idlist") or []) if isinstance(payload, dict) else []
    if not ids:
        return None
    article_map, _ = fetch_pubmed_articles(ids)
    for pmid in ids:
        article = article_map.get(str(pmid))
        if article is not None and article_doi(article) == normalized:
            return article
    return None


def crossref_title_year_author(message):
    titles = message.get("title") or []
    title = clean_ris_text(titles[0] if titles else "")
    year = ""
    for date_key in ("published-print", "published-online", "published", "issued"):
        date_obj = message.get(date_key) or {}
        parts = date_obj.get("date-parts") if isinstance(date_obj, dict) else None
        if parts and parts[0]:
            year = str(parts[0][0]) if parts[0][0] is not None else ""
            if year:
                break
    author = ""
    authors = message.get("author") or []
    if authors:
        author = clean_ris_text(str(authors[0].get("family") or authors[0].get("name") or "")).casefold()
    return title, year, author


def report_document_match_status(all_pmids, all_dois, pmid_map, doi_map):
    matched_pmids = len([p for p in all_pmids if p in pmid_map])
    matched_dois = len([d for d in all_dois if d in doi_map])
    print("Matched in Zotero for this document: {}/{} PMIDs; {}/{} DOIs.".format(
        matched_pmids, len(all_pmids), matched_dois, len(all_dois)
    ))


def resolve_document_references(all_pmids, all_dois, pubmed_articles, requested_collection=None):
    """Resolve only references in this DOCX and robustly import missing ones."""
    pmid_map, doi_map, title_map, _ = load_zotero_index()
    report_document_match_status(all_pmids, all_dois, pmid_map, doi_map)

    # Collection choice applies to ALL resolved references used by this document,
    # including items that were already in Zotero before this run.
    collection_key, collection_path = (
        resolve_requested_collection(requested_collection)
        if requested_collection is not None
        else choose_import_collection()
    )

    title_matches = []
    for pmid in sorted(all_pmids, key=int):
        if pmid in pmid_map:
            continue
        article = pubmed_articles.get(pmid)
        if article is None:
            continue
        ref = choose_title_match(
            article_title(article), title_map,
            expected_year=article_year(article),
            expected_author=article_first_author(article),
        )
        if ref:
            pmid_map[pmid] = ref
            doi = article_doi(article)
            if doi:
                doi_map.setdefault(doi, ref)
            title_matches.append(("PMID {}".format(pmid), ref))

    doi_metadata = {}
    for doi in sorted(all_dois):
        if doi in doi_map:
            continue
        article = None
        try:
            article = fetch_pubmed_article_for_doi(doi)
        except Exception as exc:
            print("  PubMed DOI lookup warning for {}: {}".format(doi, exc))
        if article is not None:
            meta = {
                "source": "pubmed", "article": article,
                "title": article_title(article), "year": article_year(article),
                "author": article_first_author(article),
            }
        else:
            try:
                message = fetch_crossref_message(doi)
                title, year, author = crossref_title_year_author(message)
                meta = {
                    "source": "crossref", "message": message,
                    "title": title, "year": year, "author": author,
                }
            except Exception as exc:
                meta = {"source": "none", "error": str(exc), "title": "", "year": "", "author": ""}
        doi_metadata[doi] = meta
        ref = choose_title_match(meta.get("title", ""), title_map, meta.get("year", ""), meta.get("author", ""))
        if ref:
            doi_map[doi] = ref
            title_matches.append(("DOI {}".format(doi), ref))

    if title_matches:
        print("Matched {} document reference(s) to existing Zotero items by title.".format(len(title_matches)))
        for identifier, ref in title_matches:
            print("  {} -> {}".format(identifier, ref.label))

    missing_pmids = [p for p in sorted(all_pmids, key=int) if p not in pmid_map]
    no_pubmed = []
    pmid_items = []
    for pmid in missing_pmids:
        article = pubmed_articles.get(pmid)
        if article is None:
            no_pubmed.append(pmid)
        else:
            pmid_items.append(pubmed_article_to_zotero_item(article))

    doi_items = []
    for doi in sorted(all_dois):
        if doi in doi_map:
            continue
        meta = doi_metadata.get(doi) or {}
        if meta.get("source") == "pubmed" and meta.get("article") is not None:
            doi_items.append(pubmed_article_to_zotero_item(meta["article"]))
        elif meta.get("source") == "crossref" and meta.get("message") is not None:
            doi_items.append(crossref_message_to_zotero_item(meta["message"], doi))

    if pmid_items:
        print("\nAdding {} missing PubMed record(s) directly to Zotero...".format(len(pmid_items)))
        try:
            created, failures = post_zotero_items(pmid_items, collection_key=collection_key)
            print("Added/confirmed {} of {} PubMed record(s).".format(created, len(pmid_items)))
            if failures:
                print("{} PubMed record(s) could not be auto-added; conversion will continue.".format(len(failures)))
        except Exception as exc:
            write_event("Could not start Zotero PubMed import: {}".format(exc))

    if doi_items:
        print("\nAdding {} missing DOI record(s) directly to Zotero...".format(len(doi_items)))
        try:
            created, failures = post_zotero_items(doi_items, collection_key=collection_key)
            print("Added/confirmed {} of {} DOI record(s).".format(created, len(doi_items)))
            if failures:
                print("{} DOI record(s) could not be auto-added; conversion will continue.".format(len(failures)))
        except Exception as exc:
            write_event("Could not start Zotero DOI import: {}".format(exc))

    fresh_pmid_map, fresh_doi_map, fresh_title_map, _ = load_zotero_index()
    fresh_pmid_map.update(pmid_map)
    fresh_doi_map.update(doi_map)
    pmid_map = fresh_pmid_map
    doi_map = fresh_doi_map

    for pmid in all_pmids:
        if pmid not in pmid_map and pubmed_articles.get(pmid) is not None:
            article = pubmed_articles[pmid]
            ref = choose_title_match(article_title(article), fresh_title_map, article_year(article), article_first_author(article))
            if ref:
                pmid_map[pmid] = ref
    for doi in all_dois:
        if doi not in doi_map:
            meta = doi_metadata.get(doi) or {}
            ref = choose_title_match(meta.get("title", ""), fresh_title_map, meta.get("year", ""), meta.get("author", ""))
            if ref:
                doi_map[doi] = ref

    report_document_match_status(all_pmids, all_dois, pmid_map, doi_map)

    if collection_key:
        document_refs = []
        seen_keys = set()
        for pmid in all_pmids:
            ref = pmid_map.get(pmid)
            if ref is not None and ref.key not in seen_keys:
                seen_keys.add(ref.key)
                document_refs.append(ref)
        for doi in all_dois:
            ref = doi_map.get(doi)
            if ref is not None and ref.key not in seen_keys:
                seen_keys.add(ref.key)
                document_refs.append(ref)
        try:
            ensure_refs_in_collection(document_refs, collection_key, collection_path)
        except Exception as exc:
            write_event("Collection assignment warning: {}".format(exc))

    return pmid_map, doi_map, doi_metadata, no_pubmed, collection_path

def write_pubmed_ris(path, pmids, article_map):
    records = []
    for pmid in sorted(set(pmids), key=int):
        article = article_map.get(pmid)
        if article is not None:
            records.append(article_to_ris(article))
    if not records:
        return 0
    path.write_text("\n\n".join(records) + "\n", encoding="utf-8")
    return len(records)


def refresh_document_maps(all_pmids, all_dois, pubmed_articles, doi_metadata):
    pmid_map, doi_map, title_map, _ = load_zotero_index()
    for pmid in all_pmids:
        if pmid in pmid_map:
            continue
        article = pubmed_articles.get(pmid)
        if article is None:
            continue
        ref = choose_title_match(article_title(article), title_map, article_year(article), article_first_author(article))
        if ref:
            pmid_map[pmid] = ref
    for doi in all_dois:
        if doi in doi_map:
            continue
        meta = doi_metadata.get(doi) or {}
        ref = choose_title_match(meta.get("title", ""), title_map, meta.get("year", ""), meta.get("author", ""))
        if ref:
            doi_map[doi] = ref
    return pmid_map, doi_map


def rewrite_docx(input_path, output_path, pmid_map, doi_map):
    with zipfile.ZipFile(input_path, "r") as zin:
        names = zin.namelist()
        raw = {name: zin.read(name) for name in names}
    parts = {}
    for name in ("word/document.xml", "word/comments.xml", "word/commentsExtended.xml", "word/commentsIds.xml", "word/commentsExtensible.xml"):
        if name in raw:
            parts[name] = raw[name].decode("utf-8")

    old_doc = parts["word/document.xml"]
    new_doc, body_stats = patch_document_xml(old_doc, pmid_map, doi_map)
    # Everything before the document body must remain byte-for-byte unchanged.
    if old_doc.split("<w:body>", 1)[0] != new_doc.split("<w:body>", 1)[0]:
        raise RuntimeError("Safety check failed: Word XML namespace/header changed.")
    parts["word/document.xml"] = new_doc
    parts, comment_stats = add_comment_citations_and_replies(parts, pmid_map, doi_map)

    # Structural XML validation before writing the ZIP.
    ET.fromstring(parts["word/document.xml"].encode("utf-8"))
    if "word/comments.xml" in parts:
        ET.fromstring(parts["word/comments.xml"].encode("utf-8"))

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name in names:
            data = parts[name].encode("utf-8") if name in parts else raw[name]
            zout.writestr(name, data)

    # Re-open exactly what was written and verify it is a valid DOCX ZIP with
    # live Zotero Word citation fields, not plain marker text.
    with zipfile.ZipFile(output_path, "r") as test_zip:
        bad = test_zip.testzip()
        if bad:
            raise RuntimeError("DOCX ZIP validation failed at {}".format(bad))
        out_doc = test_zip.read("word/document.xml").decode("utf-8")
        field_count = out_doc.count("ADDIN ZOTERO_ITEM CSL_CITATION")
        if body_stats["locations"] and field_count < body_stats["locations"]:
            raise RuntimeError("Citation field validation failed: expected at least {} live fields, found {}.".format(body_stats["locations"], field_count))

    return body_stats, comment_stats, field_count


def write_report(path, input_path, output_path, body_stats, comment_stats, field_count, full_ris_path=None, pubmed_records=0, import_collection_path=None):
    lines = [
        "DOCX -> live Zotero citation report", "=" * 36, "",
        "Input:  {}".format(input_path), "Output: {}".format(output_path),
        "PubMed RIS: {}".format(full_ris_path if full_ris_path else "not created"),
        "PubMed records fetched: {}".format(pubmed_records),
        "Document reference collection: {}".format(import_collection_path if import_collection_path else "unchanged"), "",
        "Live Zotero Word fields in output: {}".format(field_count),
        "Body citation locations converted: {}".format(body_stats["locations"]),
        "Unique body identifiers converted: {}".format(len(set(body_stats["ids"]))),
        "Comment citation items added:       {}".format(comment_stats["citations_added"]),
        "Comments updated:                   {}".format(comment_stats["comments_updated"]),
        "Threaded reply comments added:      {}".format(comment_stats["replies"]), "",
    ]
    if body_stats["manual"]:
        lines += ["LEFT FOR MANUAL REVIEW:", ""] + ["  {}".format(x) for x in body_stats["manual"]] + [""]
    unresolved_pmids = set(body_stats["missing_pmids"]) | set(comment_stats["missing_pmids"])
    unresolved_dois = set(body_stats["missing_dois"]) | set(comment_stats["missing_dois"])
    if unresolved_pmids:
        lines += ["UNRESOLVED PMIDs LEFT UNCHANGED:", ""] + ["  {}".format(p) for p in sorted(unresolved_pmids, key=int)] + [""]
    if unresolved_dois:
        lines += ["UNRESOLVED DOIs LEFT UNCHANGED:", ""] + ["  {}".format(d) for d in sorted(unresolved_dois)] + [""]
    if WRITE_EVENTS:
        lines += ["ZOTERO WRITE DIAGNOSTICS:", ""] + ["  {}".format(x) for x in WRITE_EVENTS] + [""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    if sys.version_info < (3, 8):
        sys.exit("This tool requires Python 3.8 or newer.")

    ap = argparse.ArgumentParser(
        description="Fetch PubMed metadata, resolve references in Zotero, and write live Zotero citation fields into a DOCX."
    )
    ap.add_argument("docx")
    ap.add_argument(
        "--output-dir",
        default=None,
        help="Folder for generated files (default: same folder as the input DOCX).",
    )
    ap.add_argument(
        "--no-ris",
        action="store_true",
        help="Do not create the optional PubMed RIS backup/export.",
    )
    ap.add_argument(
        "--no-report",
        action="store_true",
        help="Do not create the optional conversion report text file.",
    )
    ap.add_argument(
        "--collection",
        default=None,
        help=(
            "Put document references in this Zotero My Library collection. "
            "A unique existing name/path is used automatically; if no collection matches, "
            "a new top-level collection is created with this name."
        ),
    )
    args = ap.parse_args()
    input_path = Path(args.docx).expanduser().resolve()
    if not input_path.exists() or input_path.suffix.lower() != ".docx":
        sys.exit("Choose an existing .docx file.")

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else input_path.parent
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit("Could not create/access output folder {}: {}".format(output_dir, exc))
    if not output_dir.is_dir():
        sys.exit("Output location is not a folder: {}".format(output_dir))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_pmids, all_dois = all_identifiers_in_docx(input_path)
    print("Found in this document/comments: {} PMID(s), {} DOI(s).".format(len(all_pmids), len(all_dois)))
    pubmed_articles = {}
    full_ris_path = None
    pubmed_record_count = 0
    if all_pmids:
        print("Fetching PubMed metadata...")
        try:
            pubmed_articles, pubmed_not_returned = fetch_pubmed_articles(all_pmids)
            if not args.no_ris:
                full_ris_path = output_dir / "{}_pubmed_citations_{}.ris".format(input_path.stem, stamp)
                pubmed_record_count = write_pubmed_ris(full_ris_path, all_pmids, pubmed_articles)
                if pubmed_record_count:
                    print("Created PubMed RIS with {} record(s):".format(pubmed_record_count))
                    print(full_ris_path)
                else:
                    full_ris_path = None
            else:
                pubmed_record_count = len(pubmed_articles)
            if pubmed_not_returned:
                print("PubMed did not return: {}".format(", ".join(pubmed_not_returned)))
        except Exception as exc:
            print("PubMed metadata warning: {}".format(exc))
            print("Continuing; unresolved identifiers can be ignored rather than aborting.")

    print("\nResolving references against Zotero...")
    import_collection_path = None
    try:
        pmid_map, doi_map, doi_metadata, no_pubmed, import_collection_path = resolve_document_references(
            all_pmids, all_dois, pubmed_articles, requested_collection=args.collection
        )
    except CollectionSelectionError as exc:
        sys.exit("Collection selection failed: {}".format(exc))
    except Exception as exc:
        print("Automatic Zotero resolution warning: {}".format(exc))
        # Reads may still work even if an import failed.
        pmid_map, doi_map, _, _ = load_zotero_index()
        doi_metadata = {}
        no_pubmed = []
        import_collection_path = None

    missing_pmids = set(p for p in all_pmids if p not in pmid_map)
    missing_dois = set(d for d in all_dois if d not in doi_map)

    # Never force a manual import. The user can re-check after fixing Zotero, or
    # ignore unresolved identifiers and still get a finished DOCX. Unresolved
    # citation groups are left exactly as they were in the source document.
    while missing_pmids or missing_dois:
        print("\n{} reference identifier(s) remain unresolved.".format(len(missing_pmids) + len(missing_dois)))
        for p in sorted(missing_pmids, key=int):
            title = article_title(pubmed_articles.get(p)) if pubmed_articles.get(p) is not None else ""
            print("  PMID {}{}".format(p, " - " + title if title else ""))
        for d in sorted(missing_dois):
            meta = doi_metadata.get(d) or {}
            title = meta.get("title", "")
            print("  DOI {}{}".format(d, " - " + title if title else ""))
        answer = input("[I] Ignore and finish  [R] Re-check Zotero  [Q] Quit  (default I): ").strip().lower()
        if answer in ("", "i", "ignore"):
            print("Leaving unresolved identifier text unchanged and finishing the document.")
            break
        if answer in ("q", "quit"):
            return
        if answer in ("r", "recheck"):
            pmid_map, doi_map = refresh_document_maps(all_pmids, all_dois, pubmed_articles, doi_metadata)
            report_document_match_status(all_pmids, all_dois, pmid_map, doi_map)
            missing_pmids = set(p for p in all_pmids if p not in pmid_map)
            missing_dois = set(d for d in all_dois if d not in doi_map)
            continue
        print("Unknown choice; use I, R, or Q.")

    output_path = output_dir / "{}_zotero_citations_{}.docx".format(input_path.stem, stamp)
    report_path = None if args.no_report else output_dir / "{}_zotero_citations_{}_REPORT.txt".format(input_path.stem, stamp)

    try:
        body_stats, comment_stats, field_count = rewrite_docx(input_path, output_path, pmid_map, doi_map)
        if report_path is not None:
            write_report(
                report_path, input_path, output_path, body_stats, comment_stats,
                field_count, full_ris_path, pubmed_record_count, import_collection_path,
            )
    except Exception as exc:
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass
        sys.exit("Conversion failed: {}".format(exc))

    print("\nDONE")
    print("Created {} live Zotero Word citation field(s).".format(field_count))
    print("Body citation locations converted: {}".format(body_stats["locations"]))
    print("Comment citation items added: {}".format(comment_stats["citations_added"]))
    if body_stats["missing_pmids"] or body_stats["missing_dois"] or comment_stats["missing_pmids"] or comment_stats["missing_dois"]:
        if report_path is not None:
            print("Some unresolved identifiers were left unchanged; see the report.")
        else:
            print("Some unresolved identifiers were left unchanged.")
    print("\nCreated:")
    if full_ris_path:
        print(full_ris_path)
    print(output_path)
    if report_path is not None:
        print(report_path)
    print("\nOriginal DOCX was not changed.")


if __name__ == "__main__":
    main()
