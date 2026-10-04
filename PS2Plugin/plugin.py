"""PS2Plugin: GOG Galaxy integration for PlayStation 2 games played with PCSX2.

Author: Notimagination
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import time

import config
import plugin_log
from backend import AuthenticationServer
from galaxy.api.consts import LicenseType, LocalGameState, Platform
from galaxy.api.errors import UnknownError
from galaxy.api.plugin import Plugin, create_and_run_plugin
from galaxy.api.types import Authentication, Game, GameLibrarySettings, GameTime, LicenseInfo, LocalGame, NextStep
from PS2Client import PS2Client
from version import __version__


class PlayStation2Plugin(Plugin):
    def __init__(self, reader, writer, token):
        super().__init__(Platform.PlayStation2, __version__, reader, writer, token)
        self.config = config.Config()
        self.auth_server = AuthenticationServer()
        self.auth_server.start()
        self.games = []
        self.local_games_cache = None
        self.proc = None
        self.running_game_id = ""
        self.ps2_client = PS2Client(self)
        self.update_local_games_task = self.create_task(asyncio.sleep(0), "Update local games")
        # Galaxy 2.1.10.56 may issue one start_game_times_import request per game
        # in quick succession. Keep the plugin-side import queue so requests are
        # handled sequentially instead of triggering Galaxy's ImportInProgress state.
        self._game_time_queue = asyncio.Queue()
        self._game_time_worker_task = self.create_task(self._run_game_time_queue(), "Serialized game time imports")
        self._time_checkpoint_task = self.create_task(self._checkpoint_running_time(), "PS2 time checkpoint")

    async def authenticate(self, stored_credentials=None):
        if not stored_credentials:
            return NextStep(
                "web_session",
                {
                    "window_title": "Configure PS2 Integration",
                    "window_width": 760,
                    "window_height": 900,
                    "start_uri": f"http://localhost:{self.auth_server.port}",
                    "end_uri_regex": ".*/end.*",
                },
            )
        return self._do_auth()

    async def pass_login_credentials(self, step, credentials, cookies):
        return self._do_auth()

    def _do_auth(self):
        config_path = os.path.expandvars(config.CONFIG_LOC)
        self.config.cfg.read(config_path)
        # Remove the old optional RetroAchievements section from pre-2.2
        # installations. The new plugin no longer uses or displays it.
        if self.config.cfg.remove_section("RetroAchievements"):
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as fh:
                self.config.cfg.write(fh)
        roms = self.config.cfg.get("Paths", "roms_path", fallback="")
        self.store_credentials({"username": roms})
        return Authentication("pcsx2_user", roms)

    async def launch_game(self, game_id):
        self.config.cfg.read(os.path.expandvars(config.CONFIG_LOC))
        emu_path = self.config.cfg.get("Paths", "emu_path", fallback="").strip()
        fullscreen = self.config.cfg.getboolean("EmuSettings", "emu_fullscreen", fallback=False)
        no_gui = self.config.cfg.getboolean("EmuSettings", "emu_no_gui", fallback=False)
        use_game_config = self.config.cfg.getboolean("EmuSettings", "emu_config", fallback=False)
        config_folder = self.config.cfg.get("Paths", "config_path", fallback="").strip()
        if not emu_path or not os.path.isfile(emu_path):
            raise RuntimeError("PCSX2 executable not found: " + emu_path)
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError("A PS2 game is already running")
        game = next((item for item in self.games if item.id == game_id), None)
        if game is None:
            raise RuntimeError("PS2 game not found: " + str(game_id))
        args = [emu_path]
        if use_game_config and config_folder:
            base_name = os.path.basename(game.path)
            rom_name = base_name
            for suffix in (".iso.gz", ".bin.gz", ".iso", ".bin", ".gz"):
                if rom_name.casefold().endswith(suffix):
                    rom_name = rom_name[:-len(suffix)]
                    break
            game_config = os.path.join(config_folder, rom_name)
            if os.path.exists(os.path.join(game_config, "fullboot.ini")):
                args.append("-slowboot")
        if fullscreen:
            args.append("-fullscreen")
        if no_gui:
            args.append("-nogui")
        args.extend(["--", game.path])
        self.proc = subprocess.Popen(args, cwd=os.path.dirname(emu_path) or None)
        self.running_game_id = game.id
        self.ps2_client._set_session_start()
        data = self._load_game_times()
        self._migrate_game_times(data)
        if self._migrate_game_identity_time(game, data):
            self._save_game_times(data)
        entry = data.setdefault(game.id, {"name": game.name, "time_played": 0, "time_remainder_seconds": 0, "last_time_played": None})
        entry["name"] = game.name
        entry["_session_checkpoint_seconds"] = 0
        self._save_game_times(data)
        logging.info("DEV: PS2 emulator launched - %s", args)

    async def install_game(self, game_id):
        return

    async def uninstall_game(self, game_id):
        return

    async def prepare_local_size_context(self, game_ids):
        return self._get_local_size_dict()

    async def get_local_size(self, game_id, context):
        return context.get(game_id)

    def _get_local_size_dict(self):
        return {
            game.id: os.path.getsize(game.path)
            for game in self.games
            if os.path.exists(game.path)
        }

    def _game_times_path(self):
        return os.path.expandvars(config.GAME_TIMES_LOC)

    def _load_game_times(self):
        path = self._game_times_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as fh:
                value = json.load(fh)
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_game_times(self, data):
        path = self._game_times_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix="game_times_", suffix=".json", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=4)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                try:
                    os.unlink(temp)
                except OSError:
                    pass

    def _migrate_game_times(self, data):
        changed = False
        meta = data.get("__meta__")
        if not isinstance(meta, dict):
            meta = {"schema_version": 3, "time_unit": "minutes", "precision": "seconds"}
            data["__meta__"] = meta
            return True
        version = int(meta.get("schema_version", 0) or 0)
        if version < 2:
            for key, entry in data.items():
                if key == "__meta__" or not isinstance(entry, dict):
                    continue
                try:
                    entry["time_played"] = int(entry.get("time_played", 0) or 0) // 60
                except (TypeError, ValueError):
                    entry["time_played"] = 0
            meta["schema_version"] = 2
            meta["time_unit"] = "minutes"
            version = 2
            changed = True
        if version < 3:
            for key, entry in data.items():
                if key == "__meta__" or not isinstance(entry, dict):
                    continue
                entry.setdefault("time_remainder_seconds", 0)
            meta["schema_version"] = 3
            meta["precision"] = "seconds"
            changed = True
        return changed

    def _migrate_game_identity_time(self, game, data):
        """Move playtime stored under the disc serial or an older Galaxy ID to the current Galaxy-facing ID."""
        meta = game.metadata or {}
        current = str(game.id).upper()
        sources = []
        disc_serial = str(meta.get("disc_serial") or "").upper().strip()
        if disc_serial and disc_serial != current:
            sources.append(disc_serial)
        for legacy in meta.get("legacy_galaxy_ids") or []:
            legacy = str(legacy).upper().strip()
            if legacy and legacy != current and legacy not in sources:
                sources.append(legacy)
        changed = False
        for source_key in sources:
            source = data.get(source_key)
            if not isinstance(source, dict):
                continue
            target = data.get(game.id)
            if not isinstance(target, dict):
                target = {"name": game.name, "time_played": 0, "time_remainder_seconds": 0, "last_time_played": None}
                data[game.id] = target
            source_total = int(source.get("time_played", 0) or 0) * 60 + int(source.get("time_remainder_seconds", 0) or 0)
            target_total = int(target.get("time_played", 0) or 0) * 60 + int(target.get("time_remainder_seconds", 0) or 0)
            total = source_total + target_total
            target["name"] = game.name
            target["time_played"] = total // 60
            target["time_remainder_seconds"] = total % 60
            source_last = source.get("last_time_played")
            target_last = target.get("last_time_played")
            if source_last and (not target_last or int(source_last) > int(target_last)):
                target["last_time_played"] = source_last
            data.pop(source_key, None)
            logging.info("DEV: PS2 playtime identity migrated - %s -> %s", source_key, game.id)
            changed = True
        return changed

    def _get_games_times_dict(self):
        data = self._load_game_times()
        changed = self._migrate_game_times(data)
        result = {}
        for game in self.games:
            if self._migrate_game_identity_time(game, data):
                changed = True
            entry = data.get(game.id)
            if not isinstance(entry, dict):
                entry = {"name": game.name, "time_played": 0, "time_remainder_seconds": 0, "last_time_played": None}
                data[game.id] = entry
                changed = True
            entry["name"] = game.name
            result[game.id] = GameTime(game.id, int(entry.get("time_played", 0) or 0), entry.get("last_time_played"))
        if changed:
            self._save_game_times(data)
        return result

    async def prepare_game_times_context(self, game_ids):
        return self._get_games_times_dict()

    async def get_game_time(self, game_id, context):
        return context.get(game_id, GameTime(game_id, 0, None))

    async def _start_game_times_import(self, game_ids):
        await self._game_time_queue.put(list(game_ids or []))

    async def _run_game_time_queue(self):
        try:
            while True:
                ids = await self._game_time_queue.get()
                try:
                    context = await self.prepare_game_times_context(ids)
                    for game_id in ids:
                        try:
                            value = await self.get_game_time(game_id, context)
                            self._game_time_import_success(game_id, value)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logging.exception("DEV: PS2 game time import failed - %s", game_id)
                            self._game_time_import_failure(game_id, UnknownError())
                    self._game_times_import_finished()
                    self.game_times_import_complete()
                finally:
                    self._game_time_queue.task_done()
        except asyncio.CancelledError:
            logging.debug("DEV: serialized game time worker cancelled")
            raise

    async def _checkpoint_running_time(self):
        while True:
            try:
                await asyncio.sleep(15)
                if self.proc is None or self.proc.poll() is not None or not self.running_game_id or not self.ps2_client.start_time:
                    continue
                elapsed = int(self.ps2_client._get_session_duration())
                data = self._load_game_times()
                self._migrate_game_times(data)
                entry = data.setdefault(self.running_game_id, {"name": self.running_game_id, "time_played": 0, "time_remainder_seconds": 0, "last_time_played": None})
                checkpoint = int(entry.get("_session_checkpoint_seconds", 0) or 0)
                if elapsed - checkpoint < 30:
                    continue
                total = int(entry.get("time_played", 0) or 0) * 60 + int(entry.get("time_remainder_seconds", 0) or 0) + (elapsed - checkpoint)
                entry["time_played"] = total // 60
                entry["time_remainder_seconds"] = total % 60
                entry["_session_checkpoint_seconds"] = elapsed
                entry["last_time_played"] = int(time.time())
                self._save_game_times(data)
                self.update_game_time(GameTime(self.running_game_id, entry["time_played"], entry["last_time_played"]))
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("DEV: PS2 playtime checkpoint failed")

    def _finalize_running_time(self):
        game_id = self.running_game_id
        if not game_id or not self.ps2_client.start_time:
            return
        elapsed = int(self.ps2_client._get_session_duration())
        data = self._load_game_times()
        self._migrate_game_times(data)
        entry = data.setdefault(game_id, {"name": game_id, "time_played": 0, "time_remainder_seconds": 0, "last_time_played": None})
        checkpoint = int(entry.pop("_session_checkpoint_seconds", 0) or 0)
        remaining = max(0, elapsed - checkpoint)
        total = int(entry.get("time_played", 0) or 0) * 60 + int(entry.get("time_remainder_seconds", 0) or 0) + remaining
        entry["time_played"] = total // 60
        entry["time_remainder_seconds"] = total % 60
        entry["last_time_played"] = int(time.time())
        entry["name"] = next((game.name for game in self.games if game.id == game_id), entry.get("name", game_id))
        self._save_game_times(data)
        self.update_game_time(GameTime(game_id, entry["time_played"], entry["last_time_played"]))
        logging.info("DEV: PS2 game time finalized - %s: elapsed=%ds total=%dmin", game_id, elapsed, entry["time_played"])

    def _local_games_list(self):
        result = []
        for game in self.games:
            state = LocalGameState.Installed
            if self.running_game_id == game.id:
                state |= LocalGameState.Running
            result.append(LocalGame(game.id, state))
        return result

    def tick(self):
        self._check_emu_status()
        if self.local_games_cache is not None and self.update_local_games_task.done():
            self.update_local_games_task = self.create_task(self._update_local_games(), "Update local games")

    def _check_emu_status(self):
        if self.proc is None:
            return
        try:
            if self.proc.poll() is not None:
                self.ps2_client._set_session_end()
                self._finalize_running_time()
                self.proc = None
                self.running_game_id = ""
                self.ps2_client.start_time = 0.0
                self.ps2_client.end_time = 0.0
        except Exception:
            logging.exception("DEV: Error checking PCSX2 process status")
            self.proc = None
            self.running_game_id = ""
            self.ps2_client.start_time = 0.0
            self.ps2_client.end_time = 0.0

    async def _update_local_games(self):
        new_list = self._local_games_list()
        old_list = self.local_games_cache or []
        for game in self.ps2_client._get_state_changes(old_list, new_list):
            self.update_local_game_status(game)
        self.local_games_cache = new_list
        await asyncio.sleep(5)

    async def get_owned_games(self):
        self.config.cfg.read(os.path.expandvars(config.CONFIG_LOC))
        self.games = await asyncio.to_thread(self.ps2_client._get_games_read_iso)
        return [Game(game.id, game.name, [], LicenseInfo(LicenseType.SinglePurchase, None)) for game in self.games]

    async def get_game_library_settings(self, game_id, context):
        # Community integrations do not control GOG GamesDB visibility flags.
        # Explicitly mark our locally discovered PS2 entries as visible so a
        # stale/hidden per-game setting in Galaxy does not hide them again.
        return GameLibrarySettings(game_id, None, False)

    async def get_local_games(self):
        self.local_games_cache = self._local_games_list()
        return self.local_games_cache

    async def shutdown(self):
        if self._game_time_worker_task and not self._game_time_worker_task.done():
            self._game_time_worker_task.cancel()
        if self._time_checkpoint_task and not self._time_checkpoint_task.done():
            self._time_checkpoint_task.cancel()
        if self.proc is not None and self.proc.poll() is not None:
            self._check_emu_status()
        self.auth_server.httpd.shutdown()


def _keep_galaxy_log():
    parser = config.Config().cfg
    parser.read(os.path.expandvars(config.CONFIG_LOC))
    return parser.getboolean("Logging", "keep_galaxy_log", fallback=False)


def main():
    try:
        plugin_log.setup_logging(keep_galaxy_log=_keep_galaxy_log())
    except Exception:
        pass  # logging must never prevent the plugin from starting
    logging.info("PS2Plugin %s starting", __version__)
    create_and_run_plugin(PlayStation2Plugin, sys.argv)


if __name__ == "__main__":
    main()
