import json
import logging
import os
import re
import tempfile
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import config
from version import __version__

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
RUNTIME_DIR = os.path.expandvars(config.DATABASE_DIR)

GAMEDB_TITLES_URL = "https://github.com/niemasd/GameDB-PS2/releases/latest/download/PS2.titles.json"
OPL_DB_URL = "https://raw.githubusercontent.com/GDX-X/OPL-Games-Infos-Database-Project/refs/heads/main/PS2DB_EN.xml"
COVER_URL = "https://raw.githubusercontent.com/xlenore/ps2-covers/main/covers/default/{serial}.jpg"
COVER_3D_URL = "https://raw.githubusercontent.com/xlenore/ps2-covers/main/covers/3d/{serial}.png"

TITLE_CACHE_PATH = os.path.join(RUNTIME_DIR, "gamedb_titles_cache.json")
METADATA_CACHE_PATH = os.path.join(RUNTIME_DIR, "metadata_cache.json")
SEED_TITLE_PATH = os.path.join(DATA_DIR, "ps2_titles_seed.json")
SEED_METADATA_PATH = os.path.join(DATA_DIR, "ps2_metadata_seed.json")

CACHE_TTL = 14 * 24 * 60 * 60
TIMEOUT = 12
MAX_TITLE_BYTES = 12 * 1024 * 1024
MAX_METADATA_BYTES = 32 * 1024 * 1024
PS2_SERIAL_RE = re.compile(r"\b([A-Z]{4})[-_](\d{3})[._]?(\d{2})\b", re.I)
BOGUS_SERIALS = {"SLUS-00000", "SLUS-12345", "SLUS-99999"}
_MEMORY_EXTERNAL = None


def normalize_serial(value):
    if not value:
        return None
    match = PS2_SERIAL_RE.search(str(value).upper())
    if not match:
        return None
    serial = f"{match.group(1).upper()}-{match.group(2)}{match.group(3)}"
    return None if serial in BOGUS_SERIALS else serial


def normalize_title(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).casefold()
    value = value.replace("&", " and ")
    # Normalize the common PS2 title ordering "Sims 2, The - Castaway"
    # to the same key as "The Sims 2: Castaway".
    value = re.sub(r",\s*the\b", " ", value)
    value = re.sub(r"\[[^\]]*\]", " ", value)
    value = re.sub(r"\b(?:disc|disk|cd|dvd)\s*\d+(?:\s*of\s*\d+)?\b", " ", value)
    value = re.sub(r"\b(?:demo|trial|beta|prototype|sample|platinum|greatest hits)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    words = [w for w in value.split() if w]
    if words and words[0] == "the":
        words = words[1:]
    if words and words[-1] == "the":
        words = words[:-1]
    return " ".join(words)


def normalize_title_strict(value):
    """Normalization used for ranking aliases; edition qualifiers remain visible."""
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r",\s*the\b", " ", value)
    value = re.sub(r"\b(?:disc|disk|cd|dvd)\s*\d+(?:\s*of\s*\d+)?\b", " ", value)
    value = re.sub(r"\b(?:demo|trial|beta|prototype|sample|platinum|greatest hits)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    words = [w for w in value.split() if w and w not in {"the"}]
    return " ".join(words)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def _save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="ps2db_", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _download(url, max_bytes, label):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"PS2Plugin/{__version__}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"{label} database exceeds safety limit")
    return raw


def _parse_titles(raw):
    payload = json.loads(raw.decode("utf-8-sig", "replace"))
    candidates = payload.get("entries") if isinstance(payload, dict) and isinstance(payload.get("entries"), dict) else payload
    result = {}
    if isinstance(candidates, dict):
        for key, value in candidates.items():
            if isinstance(value, dict):
                serial = normalize_serial(value.get("serial") or value.get("id") or key)
                title = value.get("title") or value.get("name")
            else:
                serial = normalize_serial(key)
                title = value
            if serial and title:
                result[serial] = {"serial": serial, "title": str(title).strip()}
    elif isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict):
                continue
            serial = normalize_serial(item.get("serial") or item.get("id"))
            title = item.get("title") or item.get("name")
            if serial and title:
                result[serial] = {"serial": serial, "title": str(title).strip()}
    if not result:
        raise ValueError("GameDB title database contained no usable records")
    return result


def _clean_xml_tag(tag):
    return tag.rsplit("}", 1)[-1].casefold().replace("-", "_").replace(" ", "_")


def _text(value):
    return " ".join(str(value or "").split()).strip()


def _parse_opl(raw):
    root = ET.fromstring(raw)
    records = {}
    serial_keys = ("serial", "gameid", "game_id", "id", "boot")
    title_keys = ("title", "name", "gametitle", "game_title")
    for element in root.iter():
        children = list(element)
        if not children:
            continue
        fields = {}
        for child in children:
            key = _clean_xml_tag(child.tag)
            value = _text("".join(child.itertext()))
            if value and key not in fields:
                fields[key] = value
        for key, value in element.attrib.items():
            fields.setdefault(_clean_xml_tag(key), _text(value))
        serial = next((normalize_serial(fields.get(key)) for key in serial_keys if fields.get(key)), None)
        title = next((fields.get(key) for key in title_keys if fields.get(key)), "")
        if not serial or not title:
            continue
        record = {
            "serial": serial,
            "title": title,
            "region": fields.get("region", ""),
            "release": fields.get("release") or fields.get("released") or fields.get("release_date", ""),
            "developer": fields.get("developer") or fields.get("developers", ""),
            "publisher": fields.get("publisher") or fields.get("publishers", ""),
            "genre": fields.get("genre") or fields.get("genres", ""),
            "description": fields.get("description") or fields.get("desc") or fields.get("synopsis", ""),
            "players": fields.get("players") or fields.get("player") or fields.get("num_players", ""),
            "parental": fields.get("parental") or fields.get("esrb", ""),
            "rating": fields.get("rating") or fields.get("score", ""),
        }
        record = {k: v for k, v in record.items() if v not in (None, "")}
        previous = records.get(serial)
        if previous is None or len(record) > len(previous):
            records[serial] = record
    if not records:
        raise ValueError("OPL database contained no usable records")
    return records


def _seed_payload(path, kind):
    data = _read_json(path)
    if not isinstance(data, dict):
        return {"schema_version": 1, "fetched_at": 0, "entries": {}}
    entries = data.get("entries") if isinstance(data.get("entries"), dict) else data
    return {
        "schema_version": 1,
        "fetched_at": int(data.get("fetched_at", 0) or 0),
        "entries": entries if isinstance(entries, dict) else {},
    }


def _load_runtime_or_seed(runtime_path, seed_path, kind):
    runtime = _read_json(runtime_path)
    if isinstance(runtime, dict) and isinstance(runtime.get("entries"), dict) and runtime["entries"]:
        return runtime
    seed = _seed_payload(seed_path, kind)
    if seed["entries"]:
        _save_json(runtime_path, seed)
    return seed


def _fresh(payload):
    try:
        stamp = int(payload.get("fetched_at", 0) or 0)
    except (TypeError, ValueError):
        stamp = 0
    return bool(stamp and int(time.time()) - stamp <= CACHE_TTL)


def _refresh_one(kind):
    if kind == "titles":
        raw = _download(GAMEDB_TITLES_URL, MAX_TITLE_BYTES, "GameDB-PS2 titles")
        entries = _parse_titles(raw)
        payload = {"schema_version": 1, "fetched_at": int(time.time()), "entries": entries}
        _save_json(TITLE_CACHE_PATH, payload)
        logging.info("DEV: PS2 GameDB titles cached - %d serials", len(entries))
        return payload
    raw = _download(OPL_DB_URL, MAX_METADATA_BYTES, "OPL metadata")
    entries = _parse_opl(raw)
    payload = {"schema_version": 1, "fetched_at": int(time.time()), "entries": entries}
    _save_json(METADATA_CACHE_PATH, payload)
    logging.info("DEV: PS2 OPL metadata cached - %d serials", len(entries))
    return payload


def _safe_refresh(title_payload, metadata_payload):
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        if not _fresh(title_payload) or len(title_payload.get("entries", {})) < 1000:
            futures[pool.submit(_refresh_one, "titles")] = "titles"
        if not _fresh(metadata_payload) or len(metadata_payload.get("entries", {})) < 1000:
            futures[pool.submit(_refresh_one, "metadata")] = "metadata"
        for future, kind in [(f, futures[f]) for f in list(futures)]:
            try:
                result = future.result()
            except Exception as exc:
                logging.warning("DEV: PS2 %s database refresh unavailable - %s", kind, exc)
                continue
            if kind == "titles":
                title_payload = result
            else:
                metadata_payload = result
    return title_payload, metadata_payload


def _merge_sources(titles, metadata, gameindex):
    merged = {}
    for serial, item in (titles or {}).items():
        serial = normalize_serial(serial)
        if serial and isinstance(item, dict):
            merged[serial] = dict(item)
    for serial, item in (metadata or {}).items():
        serial = normalize_serial(serial)
        if not serial or not isinstance(item, dict):
            continue
        target = merged.setdefault(serial, {"serial": serial})
        for key, value in item.items():
            if value in (None, ""):
                continue
            if key == "title" and target.get("title"):
                if str(value).strip() != str(target["title"]).strip():
                    target.setdefault("aliases", []).append(str(value).strip())
            else:
                target[key] = value
    for serial, item in (gameindex or {}).items():
        serial = normalize_serial(serial)
        if not serial or not isinstance(item, dict):
            continue
        target = merged.setdefault(serial, {"serial": serial})
        target["_gameindex"] = True
        for key, value in item.items():
            if key in ("name", "title"):
                if not value:
                    continue
                value = str(value).strip()
                if not target.get("title"):
                    target["title"] = value
                elif value != str(target["title"]).strip():
                    target.setdefault("aliases", []).append(value)
            elif value not in (None, ""):
                target.setdefault(key, value)
    for serial, item in merged.items():
        item["serial"] = serial
        aliases = []
        for alias in item.get("aliases", []):
            alias = str(alias).strip()
            if alias and alias not in aliases:
                aliases.append(alias)
        item["aliases"] = aliases
        item["cover"] = COVER_URL.format(serial=serial)
        item["cover_3d"] = COVER_3D_URL.format(serial=serial)
    return merged


def load_catalogs(gameindex=None):
    global _MEMORY_EXTERNAL
    if _MEMORY_EXTERNAL is None:
        titles = _load_runtime_or_seed(TITLE_CACHE_PATH, SEED_TITLE_PATH, "titles")
        metadata = _load_runtime_or_seed(METADATA_CACHE_PATH, SEED_METADATA_PATH, "metadata")
        titles, metadata = _safe_refresh(titles, metadata)
        _MEMORY_EXTERNAL = _merge_sources(
            titles.get("entries", {}),
            metadata.get("entries", {}),
            {},
        )
        logging.info("DEV: PS2 external database ready - %d serials", len(_MEMORY_EXTERNAL))
    if gameindex:
        return _merge_sources(_MEMORY_EXTERNAL, {}, gameindex)
    return _MEMORY_EXTERNAL


def build_title_index(database):
    index = {}
    for serial, record in (database or {}).items():
        if not isinstance(record, dict):
            continue
        values = []
        aliases = record.get("aliases")
        if isinstance(aliases, (list, tuple, set)):
            values.extend(aliases)
        for value in (record.get("title"), record.get("name")):
            if value:
                values.append(value)
        for value in values:
            key = normalize_title(value)
            if key:
                index.setdefault(key, []).append(serial)
    for key, values in list(index.items()):
        index[key] = list(dict.fromkeys(values))
    return index
