import difflib
import gzip
import hashlib
import json
import logging
import os
import re
import struct
import time

import config
from definitions import PS2Game
from ps2_database import build_title_index, load_catalogs, normalize_serial, normalize_title, normalize_title_strict

SUPPORTED_EXTENSIONS = {".iso", ".bin", ".gz"}

# Some PS2 discs never show up in the Galaxy library even though the plugin
# reports them. Cause (checked against gamesdb.gog.com): GOG's GamesDB classifies
# these releases as type "dlc" (expansions with a parent game), and Galaxy does
# not list a DLC-typed release as a standalone game.
#
# Changing only the external ID does NOT help: GOG attaches any new ID to the
# same DLC entry by *title*. The alternate PAL serials, PS2-LOCAL-<serial> and
# PS2LOCAL-<serial> are all registered to the same hidden game. So for these
# discs the plugin reports a new Galaxy ID AND a slightly different title that
# GOG's title matching does not resolve to the DLC entry. The real disc serial
# is kept in the metadata and the real ISO path is still what PCSX2 launches.
#
# Overridable in config.ini:
#   [Overrides]
#   extra_serials = SLUS-12345, SLES-67890   ; more DLC-typed discs
#   id_prefix = PS2X-                        ; Galaxy ID = prefix + serial
#   title_suffix =  - PS2 Edition            ; appended to the Galaxy title
GALAXY_DLC_TYPED_SERIALS = {
    "SLUS-21664",  # The Sims 2: Castaway (GamesDB: dlc, parent The Sims 2)
    "SLUS-21536",  # The Sims 2: Pets (GamesDB: dlc, parent The Sims 2)
    "SLUS-21726",  # Samurai Warriors 2: Xtreme Legends (GamesDB: dlc)
}
STANDALONE_ID_PREFIX = "PS2X-"
STANDALONE_TITLE_SUFFIX = " - PS2 Edition"
# Galaxy IDs that earlier plugin versions exposed for these discs. Playtime
# stored under them is merged into the new ID.
LEGACY_GALAXY_IDS = {
    "SLUS-21664": ("SLES-54903",),
    "SLUS-21536": ("SLES-54347",),
}

INVALID_SERIALS = {"SLUS-00000", "SLUS-12345", "SLUS-99999"}
ROOT_EXEC_RE = re.compile(r"^([A-Z]{4})[-_](\d{3})[._]?(\d{2})(?:;\d+)?$", re.I)
BOOT_LINE_RE = re.compile(r"(?im)^\s*BOOT(?:1|2)?\s*=\s*(?:cdrom0?|cdrom)\s*:\s*([^\r\n]+)")
PVD_MARKER = b"\x01CD001\x01"
ISO_SECTOR = 2048
PVD_SECTOR = 16
# The PVD sits at sector 16, so the first 256 KiB almost always contains it. Only
# images with unusual layouts (long pregaps) need the larger 16 MiB window.
PVD_PROBE_BYTES = 256 * 1024
PVD_MAX_BYTES = 16 * 1024 * 1024


def _strip_image_extensions(value):
    name = os.path.basename(str(value or ""))
    for _ in range(2):
        suffix = os.path.splitext(name)[1].casefold()
        if suffix in SUPPORTED_EXTENSIONS:
            name = name[: -len(suffix)]
        else:
            break
    return name


class DiscImageReader:
    _LAYOUTS = (
        (2048, 0),
        (2352, 16), (2352, 24), (2352, 0),
        (2448, 16), (2448, 24), (2448, 0),
    )

    def __init__(self, path):
        self.path = path
        self.compressed = str(path).casefold().endswith(".gz")
        self.sector_size = None
        self.data_offset = 0
        self.track_base = 0
        self.pvd = None
        self._gz = None
        self._root_entries = None

    def _open_handle(self):
        if self.compressed:
            if self._gz is None:
                self._gz = gzip.open(self.path, "rb")
            return self._gz
        return open(self.path, "rb")

    def _read_at(self, offset, size):
        if offset < 0 or size <= 0:
            return b""
        fh = self._open_handle()
        try:
            fh.seek(offset)
            return fh.read(size)
        finally:
            if not self.compressed:
                fh.close()

    def _detect_pvd_in_prefix(self, prefix):
        search_from = 0
        while True:
            marker = prefix.find(PVD_MARKER, search_from)
            if marker < 0:
                return False
            for sector_size, data_offset in self._LAYOUTS:
                track_base = marker - (PVD_SECTOR * sector_size + data_offset)
                if track_base < 0 or track_base % sector_size:
                    continue
                pvd = prefix[marker:marker + ISO_SECTOR]
                if len(pvd) < ISO_SECTOR:
                    continue
                if pvd[0:7] != PVD_MARKER:
                    continue
                self.sector_size = sector_size
                self.data_offset = data_offset
                self.track_base = track_base
                self.pvd = pvd
                return True
            search_from = marker + 1

    def open(self):
        for size in (PVD_PROBE_BYTES, PVD_MAX_BYTES):
            try:
                prefix = self._read_at(0, size)
            except (OSError, EOFError, gzip.BadGzipFile):
                return False
            if not prefix:
                return False
            if self._detect_pvd_in_prefix(prefix):
                return True
            if len(prefix) < size:
                return False  # the whole image was already scanned
        return False

    def _abs_sector_offset(self, lba):
        return self.track_base + int(lba) * self.sector_size + self.data_offset

    def _read_sector_data(self, lba, count=1):
        """Return `count` consecutive 2048-byte data blocks starting at `lba`."""
        if not self.sector_size or count <= 0:
            return b""
        count = int(count)
        try:
            if self.sector_size == ISO_SECTOR and not self.data_offset:
                return self._read_at(self._abs_sector_offset(lba), count * ISO_SECTOR)
            # Raw images (2352/2448 bytes per sector): skip the sync/header/ECC
            # bytes of every sector instead of reading across them.
            raw = self._read_at(self.track_base + int(lba) * self.sector_size, count * self.sector_size)
        except (OSError, EOFError, gzip.BadGzipFile):
            return b""
        start = self.data_offset
        return b"".join(
            raw[i * self.sector_size + start:i * self.sector_size + start + ISO_SECTOR]
            for i in range(count)
        )

    def volume_id(self):
        return self.pvd[40:72].decode("ascii", "ignore").strip() if self.pvd else ""

    def root_record(self):
        if not self.pvd or len(self.pvd) < 190:
            return None
        try:
            lba = struct.unpack_from("<I", self.pvd, 156 + 2)[0]
            size = struct.unpack_from("<I", self.pvd, 156 + 10)[0]
        except struct.error:
            return None
        if lba <= 0 or size <= 0 or size > 16 * 1024 * 1024:
            return None
        return lba, size

    @staticmethod
    def _decode_name(raw):
        return raw.decode("ascii", "ignore").strip()

    def _parse_dir(self, data):
        entries = []
        pos = 0
        while pos < len(data):
            length = data[pos]
            if length == 0:
                pos = ((pos // ISO_SECTOR) + 1) * ISO_SECTOR
                continue
            if length < 34 or pos + length > len(data):
                break
            name_len = data[pos + 32]
            raw_name = data[pos + 33:pos + 33 + name_len]
            name = self._decode_name(raw_name)
            if name not in ("\x00", "\x01", ""):
                try:
                    lba = struct.unpack_from("<I", data, pos + 2)[0]
                    size = struct.unpack_from("<I", data, pos + 10)[0]
                except struct.error:
                    lba, size = 0, 0
                entries.append({
                    "name": name,
                    "lba": lba,
                    "size": size,
                    "is_dir": bool(data[pos + 25] & 0x02),
                })
            pos += length
        return entries

    def root_entries(self):
        if self._root_entries is not None:
            return self._root_entries
        record = self.root_record()
        if not record:
            self._root_entries = []
            return self._root_entries
        lba, size = record
        sectors = (size + ISO_SECTOR - 1) // ISO_SECTOR
        data = self._read_sector_data(lba, sectors)[:size]
        self._root_entries = self._parse_dir(data)
        return self._root_entries

    @staticmethod
    def _clean_iso_name(value):
        return str(value or "").split(";", 1)[0].rstrip(".").casefold()

    def read_root_file(self, name):
        wanted = self._clean_iso_name(name)
        for entry in self.root_entries():
            if entry["is_dir"]:
                continue
            if self._clean_iso_name(entry["name"]) == wanted:
                count = (entry["size"] + ISO_SECTOR - 1) // ISO_SECTOR
                return self._read_sector_data(entry["lba"], count)[:entry["size"]]
        return b""

    def read_path(self, path):
        value = str(path or "").split(":", 1)[-1].replace("/", "\\").lstrip("\\")
        parts = [part for part in value.split("\\") if part]
        if not parts:
            return b""
        current_dir = self.root_entries()
        for index, part in enumerate(parts):
            wanted = self._clean_iso_name(part)
            found = next((e for e in current_dir if self._clean_iso_name(e["name"]) == wanted), None)
            if found is None:
                return self.read_root_file(part) if index == len(parts) - 1 else b""
            if index == len(parts) - 1:
                count = (found["size"] + ISO_SECTOR - 1) // ISO_SECTOR
                return self._read_sector_data(found["lba"], count)[:found["size"]]
            if not found["is_dir"]:
                return b""
            count = (found["size"] + ISO_SECTOR - 1) // ISO_SECTOR
            data = self._read_sector_data(found["lba"], count)[:found["size"]]
            current_dir = self._parse_dir(data)
        return b""

    def close(self):
        if self._gz is not None:
            try:
                self._gz.close()
            except Exception:
                pass
            self._gz = None


class PS2Client:
    def __init__(self, plugin):
        self.plugin = plugin
        self.games = []
        self.start_time = 0.0
        self.end_time = 0.0

    @staticmethod
    def _valid_serial(value):
        serial = normalize_serial(value)
        return None if serial in INVALID_SERIALS else serial

    @staticmethod
    def _serial_from_exec_name(value):
        cleaned = str(value or "").strip().split(";", 1)[0]
        match = ROOT_EXEC_RE.match(cleaned)
        return PS2Client._valid_serial(f"{match.group(1)}-{match.group(2)}{match.group(3)}") if match else None

    @staticmethod
    def _serial_from_boot_path(value):
        value = str(value or "").strip().strip('"').strip("'")
        value = value.split(";", 1)[0].strip()
        # Accept SLUS_215.36, SLUS-215.36, SLUS_21536 and equivalent forms.
        match = re.search(r"([A-Z]{4})[-_](\d{3})[._]?(\d{2})(?:\.[A-Z0-9]+)?$", value, re.I)
        if not match:
            return None
        return PS2Client._valid_serial(f"{match.group(1)}-{match.group(2)}{match.group(3)}")

    @staticmethod
    def _parse_boot_line(data):
        if not data:
            return None, None
        text = data.decode("ascii", "ignore")
        # SYSTEM.CNF syntax varies slightly between releases: BOOT may use
        # cdrom:, cdrom0:, cdrom1:, mixed case, spaces and a ;1 version suffix.
        # Search all BOOT lines and accept the first valid PS2 serial.
        for match in BOOT_LINE_RE.finditer(text):
            target = match.group(1).strip().strip('"').strip("'")
            serial = PS2Client._serial_from_boot_path(target)
            if serial:
                return serial, target
        # Some images omit the conventional BOOT= prefix formatting. Fall back
        # to scanning SYSTEM.CNF for a PS2 serial-looking executable name.
        serial_match = re.search(r"\b([A-Z]{4})[-_](\d{3})[._](\d{2})(?:;\d+)?\b", text, re.I)
        if serial_match:
            serial = PS2Client._valid_serial(f"{serial_match.group(1)}-{serial_match.group(2)}{serial_match.group(3)}")
            if serial:
                return serial, serial_match.group(0)
        return None, None

    @staticmethod
    def _crc_xor32(data):
        if not data or data[:4] != b"\x7fELF":
            return None
        limit = len(data) - len(data) % 4
        # XOR of all little-endian 32-bit words. Done by folding one big integer
        # in half (zero padding does not change a XOR), which is much faster than
        # a Python loop over every word.
        value = int.from_bytes(data[:limit], "little")
        words = 1
        while words < limit // 4:
            words <<= 1
        half = words * 16
        while half >= 32:
            value = (value & ((1 << half) - 1)) ^ (value >> half)
            half >>= 1
        return value & 0xFFFFFFFF

    @staticmethod
    def _root_target_name(value):
        return str(value or "").replace("/", "\\").rsplit("\\", 1)[-1].split(";", 1)[0].rstrip(".").casefold()

    def _extract_disc_identity(self, path, database):
        reader = DiscImageReader(path)
        try:
            if reader.open():
                system = reader.read_root_file("SYSTEM.CNF")
                boot_serial, boot_target = self._parse_boot_line(system)
                target_elf = reader.read_path(boot_target) if boot_target else b""
                boot_target_file_exists = bool(target_elf)

                root_serials = []
                root_by_name = {}
                for entry in reader.root_entries():
                    serial = self._serial_from_exec_name(entry.get("name"))
                    if serial:
                        root_by_name[self._root_target_name(entry["name"])] = serial
                        if serial not in root_serials:
                            root_serials.append(serial)

                boot_name = self._root_target_name(boot_target)
                boot_root_serial = root_by_name.get(boot_name)
                known_root_serials = [serial for serial in root_serials if serial in database]

                selected = None
                source = None
                if boot_root_serial and boot_serial == boot_root_serial:
                    selected, source = boot_root_serial, "system_cnf_root_exec"
                elif boot_target_file_exists and boot_serial and boot_serial in database:
                    selected, source = boot_serial, "system_cnf_boot_target"
                elif len(known_root_serials) == 1:
                    selected, source = known_root_serials[0], "root_executable"
                    if boot_serial and boot_serial != selected:
                        logging.warning(
                            "DEV: PS2 identity conflict - %s BOOT=%s root=%s; using known root executable",
                            os.path.basename(path), boot_serial, selected,
                        )
                elif boot_serial and boot_serial in database:
                    selected, source = boot_serial, "system_cnf"
                elif known_root_serials:
                    selected, source = known_root_serials[0], "root_executable"
                elif boot_serial:
                    selected, source = boot_serial, "system_cnf_unlisted"
                elif root_serials:
                    selected, source = root_serials[0], "root_executable_unlisted"
                else:
                    volume_serial = self._valid_serial(reader.volume_id())
                    if volume_serial:
                        selected, source = volume_serial, "volume_id"

                crc = self._crc_xor32(target_elf)
                if selected:
                    return selected, source, crc

            # Last resort: require explicit SYSTEM.CNF text before accepting BOOT.
            try:
                if str(path).casefold().endswith(".gz"):
                    with gzip.open(path, "rb") as fh:
                        raw = fh.read(16 * 1024 * 1024)
                else:
                    with open(path, "rb") as fh:
                        raw = fh.read(16 * 1024 * 1024)
            except (OSError, gzip.BadGzipFile):
                raw = b""
            if b"SYSTEM.CNF" in raw.upper():
                raw_serial, _ = self._parse_boot_line(raw)
                if raw_serial:
                    return raw_serial, "raw_system_cnf", None
        finally:
            reader.close()
        return None, None, None

    @classmethod
    def _normalize_title(cls, value):
        return normalize_title(_strip_image_extensions(value))

    @staticmethod
    def _region_hint(filename):
        text = str(filename or "").casefold()
        if re.search(r"\b(?:ntsc[-_ ]?u|usa|u[.]s[.]a[.]|us)\b", text):
            return 0
        if re.search(r"\b(?:pal|eur|europe|eu)\b", text):
            return 1
        if re.search(r"\b(?:ntsc[-_ ]?j|japan|jpn|jp)\b", text):
            return 2
        return None

    @staticmethod
    def _region_rank(game_id, record, hint=None):
        gid = str(game_id).upper()
        region = str(record.get("region", "")).upper()
        title = str(record.get("title") or record.get("name") or "").casefold()
        aliases = record.get("aliases", [])
        alias_text = " ".join(str(item) for item in aliases) if isinstance(aliases, (list, tuple, set)) else ""
        demo_text = title + " " + alias_text.casefold()
        demo_rank = 1 if re.search(r"\b(?:demo|trial|beta|prototype|sample)\b", demo_text) else 0
        if "NTSC-U" in region or gid.startswith(("SLUS-", "SCUS-")):
            region_rank = 0
        elif "PAL" in region or gid.startswith(("SLES-", "SCES-")):
            region_rank = 1
        elif "NTSC-J" in region or gid.startswith(("SLPM-", "SLPS-", "SCPS-", "SCAJ-")):
            region_rank = 2
        else:
            region_rank = 3
        hint_penalty = 0 if hint is None or region_rank == hint else 10
        source_rank = 0 if record.get("_gameindex") else 1
        return demo_rank, hint_penalty, source_rank, region_rank, gid

    def _filename_match(self, filename, title_index, database):
        key = self._normalize_title(filename)
        if not key:
            return None
        hint = self._region_hint(filename)
        exact = list(dict.fromkeys(title_index.get(key, [])))
        if exact:
            query_token_count = len(key.split())
            alias_candidates = []
            canonical_candidates = []
            for gid in exact:
                record = database.get(gid, {})
                aliases = record.get("aliases", [])
                if not isinstance(aliases, (list, tuple, set)):
                    aliases = []
                extras = []
                for alias in aliases:
                    if normalize_title(alias) == key:
                        strict_alias = normalize_title_strict(alias)
                        extras.append(max(0, len(strict_alias.split()) - query_token_count))
                if extras:
                    alias_candidates.append((min(extras), gid))
                    continue
                canonical = normalize_title(record.get("title") or record.get("name"))
                if canonical == key:
                    canonical_candidates.append(gid)
            if canonical_candidates:
                # A canonical exact title is stronger than a re-release/edition alias.
                # This prevents entries such as "Black [EA Best Hits]" from stealing
                # the plain PS2 release just because its alias normalizes to "Black".
                return sorted(canonical_candidates, key=lambda gid: self._region_rank(gid, database.get(gid, {}), hint))[0]
            if alias_candidates:
                alias_candidates.sort(key=lambda item: (item[0], self._region_rank(item[1], database.get(item[1], {}), hint)))
                return alias_candidates[0][1]
            return sorted(exact, key=lambda gid: self._region_rank(gid, database.get(gid, {}), hint))[0]

        query_tokens = set(key.split())
        containment = []
        for title_key, ids in title_index.items():
            candidate_tokens = set(title_key.split())
            if query_tokens and query_tokens.issubset(candidate_tokens):
                extra = len(candidate_tokens) - len(query_tokens)
                if extra <= 3:
                    score = len(query_tokens) / max(1, len(candidate_tokens))
                    containment.append((score, -extra, title_key, ids))
        if containment:
            containment.sort(reverse=True)
            top = containment[0]
            tied = [item for item in containment if abs(item[0] - top[0]) < 0.01 and item[1] == top[1]]
            unique = []
            for _, _, _, ids in tied:
                for gid in ids:
                    if gid not in unique:
                        unique.append(gid)
            if unique:
                return sorted(unique, key=lambda gid: self._region_rank(gid, database.get(gid, {}), hint))[0]

        best = []
        for title_key, ids in title_index.items():
            ratio = difflib.SequenceMatcher(None, key, title_key).ratio()
            if ratio < 0.93:
                continue
            a = set(key.split())
            b = set(title_key.split())
            overlap = len(a & b) / max(1, len(a | b))
            if ratio >= 0.96 or overlap >= 0.85:
                best.append((ratio, overlap, title_key, ids))
        if not best:
            return None
        best.sort(key=lambda item: (item[0], item[1]), reverse=True)
        top_ratio, top_overlap, _, ids = best[0]
        if len(best) > 1 and best[1][0] > top_ratio - 0.015 and best[1][1] > top_overlap - 0.05:
            return None
        return sorted(ids, key=lambda gid: self._region_rank(gid, database.get(gid, {}), hint))[0]

    @staticmethod
    def _display_title(filename):
        stem = _strip_image_extensions(filename)
        stem = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)
        stem = re.sub(r"\b(?:disc|disk|cd|dvd)\s*\d+(?:\s*of\s*\d+)?\b", " ", stem, flags=re.I)
        return re.sub(r"\s+", " ", stem).strip(" -_") or os.path.basename(filename)

    @staticmethod
    def _fallback_id(filename):
        key = normalize_title(_strip_image_extensions(filename)) or os.path.basename(filename).casefold()
        return "PS2-NAME-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16].upper()

    @staticmethod
    def _load_gameindex():
        """Serial -> {name, region} from the bundled PCSX2 GameIndex (slimmed to the fields the plugin uses)."""
        filename = os.path.join(os.path.dirname(os.path.abspath(__file__)), "GameIndex.json")
        try:
            with open(filename, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            logging.exception("DEV: Could not load GameIndex.json")
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k).upper(): dict(v) for k, v in data.items() if isinstance(v, dict)}

    def _build_record(self, serial, gameindex, database, filename, crc):
        record = dict(database.get(serial, {})) if serial else {}
        if serial and not record:
            record = dict(gameindex.get(serial, {}))
        title = str(record.get("title") or record.get("name") or self._display_title(filename)).strip()
        record["detected_serial"] = serial
        if crc is not None:
            record["crc"] = f"{crc:08X}"
        if serial:
            record.setdefault("cover", f"https://raw.githubusercontent.com/xlenore/ps2-covers/main/covers/default/{serial}.jpg")
        return title, record

    def _standalone_overrides(self):
        """Serials/ID prefix/title suffix for DLC-typed GamesDB releases (config.ini [Overrides])."""
        serials = set(GALAXY_DLC_TYPED_SERIALS)
        prefix = STANDALONE_ID_PREFIX
        suffix = STANDALONE_TITLE_SUFFIX
        cfg = self.plugin.config.cfg
        try:
            if cfg.has_option("Overrides", "extra_serials"):
                for item in re.split(r"[,;\s]+", cfg.get("Overrides", "extra_serials", raw=True) or ""):
                    serial = normalize_serial(item)
                    if serial:
                        serials.add(serial)
            if cfg.has_option("Overrides", "id_prefix"):
                prefix = (cfg.get("Overrides", "id_prefix", raw=True) or "").strip() or prefix
            if cfg.has_option("Overrides", "title_suffix"):
                suffix = cfg.get("Overrides", "title_suffix", raw=True) or ""
        except Exception:
            logging.exception("DEV: PS2 [Overrides] section unreadable; using defaults")
        return serials, prefix, suffix

    def _get_games_read_iso(self):
        self.plugin.config.cfg.read(os.path.expandvars(config.CONFIG_LOC))
        rom_path = os.path.normpath(self.plugin.config.cfg.get("Paths", "roms_path", fallback=""))
        self.games = []
        if not os.path.isdir(rom_path):
            logging.warning("DEV: PS2 ROM folder does not exist - %s", rom_path)
            return self.games

        standalone_serials, id_prefix, title_suffix = self._standalone_overrides()

        gameindex = self._load_gameindex()
        database = load_catalogs(gameindex)
        title_index = build_title_index(database)

        found = {}
        scanned = serial_hits = filename_hits = fallback_hits = 0
        for root, dirs, files in os.walk(rom_path):
            dirs.sort(key=str.casefold)
            files.sort(key=str.casefold)
            for filename in files:
                lower = filename.casefold()
                ext = ".gz" if lower.endswith(".gz") else os.path.splitext(lower)[1]
                if ext not in SUPPORTED_EXTENSIONS:
                    continue
                scanned += 1
                path = os.path.normpath(os.path.join(root, filename))
                serial, source, crc = self._extract_disc_identity(path, database)
                if serial:
                    serial_hits += 1
                    game_id = serial
                    title, record = self._build_record(serial, gameindex, database, filename, crc)
                    record["detected_source"] = source
                    logging.debug("DEV: PS2 identity - %s -> %s (%s)%s", filename, serial, source, f" CRC={crc:08X}" if crc is not None else "")
                else:
                    game_id = self._filename_match(filename, title_index, database)
                    if game_id:
                        filename_hits += 1
                        title, record = self._build_record(game_id, gameindex, database, filename, crc)
                        record["detected_serial"] = None
                        record["detected_source"] = "filename"
                        logging.warning("DEV: PS2 filename fallback - %s -> %s", filename, game_id)
                    else:
                        fallback_hits += 1
                        game_id = self._fallback_id(filename)
                        title = self._display_title(filename)
                        record = {"detected_serial": None, "detected_source": "filename_hash"}
                        logging.warning("DEV: PS2 unresolved image; stable fallback ID - %s -> %s", filename, game_id)

                # Galaxy library visibility safeguard for DLC-typed GamesDB
                # releases (see GALAXY_DLC_TYPED_SERIALS). The real disc serial
                # stays authoritative in the metadata; only the Galaxy-facing
                # ID and title change.
                original_id = str(game_id).upper()
                if original_id in standalone_serials:
                    galaxy_id = id_prefix + original_id
                    record["disc_serial"] = original_id
                    record["galaxy_game_id"] = galaxy_id
                    record["legacy_galaxy_ids"] = list(LEGACY_GALAXY_IDS.get(original_id, ()))
                    record["galaxy_dlc_typed_workaround"] = True
                    game_id = galaxy_id
                    title = title + title_suffix
                    record["galaxy_title"] = title
                    logging.warning(
                        "DEV: PS2 GamesDB DLC-typed release exposed as standalone - %s (%s) -> %s / %r",
                        filename, original_id, galaxy_id, title,
                    )

                if game_id in found:
                    existing = found[game_id][1]
                    logging.warning("DEV: duplicate PS2 identity ignored - %s and %s -> %s", existing, path, game_id)
                    continue
                found[game_id] = (title, path, record)

        self.games = [PS2Game(gid, value[0], value[1], value[2]) for gid, value in found.items()]
        self.games.sort(key=lambda game: (game.name.casefold(), game.id))
        logging.info(
            "DEV: PS2 external metadata prepared - covers=%d/%d",
            sum(1 for game in self.games if game.metadata.get("cover")),
            len(self.games),
        )
        logging.info(
            "DEV: PS2 scan summary - scanned=%d, serial_ids=%d, filename_ids=%d, fallback_ids=%d, unresolved=%d, imported=%d",
            scanned, serial_hits, filename_hits, fallback_hits, fallback_hits, len(self.games),
        )
        return self.games

    @staticmethod
    def _get_state_changes(old_list, new_list):
        old = {x.game_id if hasattr(x, "game_id") else x.id: x for x in old_list or []}
        changed = []
        for item in new_list:
            key = item.game_id if hasattr(item, "game_id") else item.id
            if key not in old or old[key].local_game_state != item.local_game_state:
                changed.append(item)
        return changed

    def _get_session_duration(self):
        if not self.start_time:
            return 0
        end = self.end_time or time.monotonic()
        return max(0, end - self.start_time)

    def _set_session_start(self):
        self.start_time = time.monotonic()
        self.end_time = 0.0

    def _set_session_end(self):
        self.end_time = time.monotonic()
