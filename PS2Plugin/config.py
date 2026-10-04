import configparser

PLUGIN_DIR = r"%LOCALAPPDATA%\GOG.com\Galaxy\Configuration\plugins\ps2"
CONFIG_LOC = PLUGIN_DIR + r"\config.ini"
GAME_TIMES_LOC = PLUGIN_DIR + r"\game_times.json"
DATABASE_DIR = PLUGIN_DIR + r"\database"


class Config:
    def __init__(self):
        self.cfg = configparser.ConfigParser(allow_no_value=True)
        self.cfg["Paths"] = {
            "roms_path": "C:/Games/PS2",
            "emu_path": "C:/Program Files (x86)/PCSX2 1.4.0/pcsx2.exe",
            "config_path": "",
        }
        self.cfg["EmuSettings"] = {
            "emu_fullscreen": "False",
            "emu_no_gui": "False",
            "emu_config": "False",
        }
