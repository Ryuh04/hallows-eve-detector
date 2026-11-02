# Hallows' Eve Detector

A Windows app that watches your Roblox windows for the **Hallows' Eve** event and helps out while it's on:

- **Detects the event** in each window (even behind other windows), with a countdown.
- **Auto equip:** holds the characters you pick during the event, in the order you picked them, skipping any that already have Dracula, and unequips them when it ends.
- **Anti Disconnect:** reconnects after a disconnect and closes the "Welcome Back" popup.
- **Auto rebirth:** rebirths whenever your cash covers the next rebirth, with a log.
- **Trim Roblox RAM:** frees memory your Roblox windows aren't using, every 2 minutes.
- **Discord ping** (optional) with each window's private server link.

Every window has its own switches, so you can run several Roblox windows at once.

## Download

### [⬇ Download HallowsEveDetector.exe](https://github.com/Ryuh04/hallows-eve-detector/releases/latest/download/HallowsEveDetector.exe)

1. Click the link above. It always downloads the newest version (about 145 MB).
2. Put the file wherever you like, such as your Desktop.
3. Double-click it. Nothing else needs installing.

**The first time**, Windows may show *"Windows protected your PC"*, because the app isn't signed by a paid certificate. Click **More info**, then **Run anyway**. Windows remembers your choice for that file.

## Updating

Click **Check for updates** at the bottom left of the app. When there's a newer version, click **Install and restart**. The app downloads the new version, swaps itself, and opens again. Your settings, logs and private server links are kept.

The app keeps your settings in `%LOCALAPPDATA%\Hallows Eve Detector`, so you can move or replace the .exe freely.

## Using it

1. Pick the Roblox windows to watch, then **Start watching**. Windows can sit behind others, but they can't be minimised.
2. Each window gets a page with:
   - its live view;
   - switches for detection, Anti Disconnect and Auto rebirth;
   - its hotbar: click characters there to auto equip them;
   - a box for the private server link.
3. Double-click a window in the sidebar to rename it.

## Discord pings (optional)

Pings only work with a Discord bot token, which you never share. The app makes `discord_config.json` the first time it runs, in the settings folder above (or next to the Python files when running from source):

```json
{
  "bot_token": "",
  "channel": "event-ping",
  "ping_role": "ps-ping"
}
```

Put the bot's token in `bot_token`; the bot needs to be in the server with that channel and role. Without a token, everything else works and pings stay off.

## Running from the Python source (optional)

You only need this to change the code; the .exe above is the same app.

1. Install **Python 3.12 or newer** from [python.org](https://www.python.org/downloads/), ticking **"Add python.exe to PATH"**.
2. Click **Code → Download ZIP** (or clone the repository) and unzip it.
3. Double-click **`Install.bat`**. It installs the packages and puts a shortcut on your Desktop.

**Check for updates** works here too; it updates the Python files.

## Files

| File | What it is |
|---|---|
| `hallows_eve_detector.py` | Watching, equipping, rebirths, Discord (also has a console mode: `--help`) |
| `detector_ui.py` | The app window (Qt / PySide6) |
| `updater.py` | Check for updates / install them from this repository |
| `build_exe.ps1`, `.github/workflows/build-exe.yml` | Build the .exe; GitHub does this on every push and publishes it as a release |
| `Install.bat`, `create_shortcut.ps1` | Setup for running from source |

Your own data stays on your PC and is never uploaded: `discord_config.json`, `private_servers.json`, `window_names.json`, `rebirth_log.csv`, `ui_settings.json` and `detector_output.log`.
