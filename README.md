# GOG Galaxy PlayStation 2 Integration (Updated)

> [!IMPORTANT]
> This is a fork and modern update of the original [galaxy-integration-ps2](https://github.com/AHCoder/galaxy-integration-ps2) plugin by AHCoder, which became outdated and non-functional for current versions of GOG Galaxy.

## ✨ What's New & Improved

* **Full Compatibility:** Fully functional with recent versions of GOG Galaxy.

* **Modern UI:** Completely redesigned user interface with a sleek, modern look.

* **File Support:** Added support for `.bin` files.

* **Optimized Code:** General code cleanup and performance improvements.

* **Better Detection:** Revised core logic for superior game detection.

* **Persistent Local Config:** Settings and paths are now saved locally so your preferences are always retained.

* **Integrated Playtime:** Playtime tracking is now directly linked to the GOG system.

* **Expanded Emulator Support:** Support for multiple emulators beyond just PCSX2 (PCSX2 has the best compatibility and it's used as default).

* **Long-Term Maintenance:** Ongoing plugin maintenance with regular updates, new features, and bug fixes.

## 📌 Other emulator plugins

| Integration | Status | Achievements | Game Time | Download |
|-------------|--------|--------------|-----------|----------|
| Switch | ✅ Released | ⚠️ | ✅ | [Download](https://github.com/Notimagination/galaxy-switch-integration) |
| NES | ✅ Released | ❌(⚠️) | ✅ | [Download](https://github.com/Notimagination/galaxy-nes-integration) |
| PSP | ⏳ Planned | ⚠️ | ✅ | Download |
| WII | ⏳ Planned | ⚠️ | ✅ | Download |
| PS3 | ⏳ Planned | ❌ | ✅ | Download |
| Local games | ⏳ Planned | ❌ | ✅ | Download |

  > [!NOTE]
  > Some emulators may support achievements through [RetroAchievements](https://retroachievements.org/), provided that GOG decides to integrate with the system. If that ever happens (which I highly doubt), I’ll implement it.

## 📦 Installation Guide

1. Download the `.zip` file from this repository, or you can check the [releases](https://github.com/Notimagination/galaxy-ps2-integration-renew/releases) page for the latest updates.
   
 <img width="969" height="407" alt="step1" src="https://github.com/user-attachments/assets/4c9b1ce5-a46a-41bb-9bbf-39ea150d5f7a" />

2. Extract and move the `PS2Plugin` folder to your GOG Galaxy plugins directory:

   ```
   %localappdata%\GOG.com\Galaxy\plugins\installed
   ```

3. Open **GOG Galaxy**, go to **Settings** > **Integrations**, and look for **PlayStation 2**. Click **Connect**.

   <img width="559" height="348" alt="step3" src="https://github.com/user-attachments/assets/e73054a3-1269-49af-8e25-9ff09ccd31e2" />

4. Configure your paths:

   <img width="754" height="1675" alt="step4" src="https://github.com/user-attachments/assets/dd35bbec-1ac5-469b-858c-0b0ec4cdc3bf" />

5. Click the **Save config** button and wait for your games to import.

   <img width="1896" height="881" alt="step5" src="https://github.com/user-attachments/assets/886b08ad-f3fa-4146-a954-8a184659fb85" />

## 🎮 Requesting Game Additions

If you would like me to add support for a missing game in a future update, please provide the following details:

* **Game Name**
* **Serial Number**
* **CRC**
* **The log file** generated at `%programdata%\GOG.com\Galaxy` (`plugin-ps2-1e814707-1fe3-4e1e-86fe-1b8d1b7fac2e.log`)

🎫 **Open a ticket on the [Issues](https://github.com/Notimagination/galaxy-ps2-integration-renew/issues) page**.

## ❓ Frequently Asked Questions (FAQ)

### Q: Some of my games show up as "Unknown".

**A:** Please report the missing game by trying to run it using the "Play" button; this helps me identify it so I can potentially add it in a future update. This happens due to GOG's internal database limitations, as it's technically impossible to hook into a more reliable external database (like PCSX2 does). The only solution is for me to manually include it.

### Q: Some games have the correct name and details, but are missing box art or images.

**A:** For the same reason mentioned above. The workaround here is to manually add the images using the "Edit" section for each game within GOG Galaxy.

### Q: Some games have names and covers, but they don't match the actual game.

**A:** Again, this is because GOG relies entirely on its own closed database for console integrations instead of external APIs. The solution is to manually update the name and/or cover art via the game's "Edit" menu in GOG.

### Q: I have a certain number of games in my folder, but fewer are showing up in GOG.

**A:** This is also caused by GOG's database mapping. The most helpful thing you can do is let me know which specific games are missing (by comparing your folder to what appears in GOG) so I can add them in an update.

### Q: The plugin disconnected, and when I reconnected it, all the custom data and images I added to each game were deleted.

**A:** GOG deletes all custom data because it forces the use of its own database. The solution is to avoid disconnecting the plugin. If it does happen, you will need to reload the images manually. There is not much I can do about this or anything related to GOG's database, unless GOG decides to make this more flexible in the future.
