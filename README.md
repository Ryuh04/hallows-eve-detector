# Hallows' Eve Detector

A Windows app that watches your Roblox windows for the **Hallows' Eve** event and helps out while it's on:

- **Detects the event** in each window (even behind other windows), with a countdown.
- **Auto equip:** holds the characters you pick during the event, in the order you picked them, skipping any that already have Dracula, and unequips them when it ends.
- **Anti Disconnect:** reconnects after a disconnect and closes the "Welcome Back" popup.
- **Auto rebirth:** rebirths whenever your cash covers the next rebirth, with a log.
- **Discord ping** (optional) with each window's private server link.

Every window has its own switches, so you can run several Roblox windows at once.

## Install (for friends)

1. Install **Python 3.12 or newer** from [python.org](https://www.python.org/downloads/). In the installer, tick **"Add python.exe to PATH"**.
2. On this page, click the green **Code** button, then **Download ZIP**. Unzip it somewhere you'll keep it, such as your Documents folder.
3. Double-click **`Install.bat`** in that folder. It installs the Python packages the app needs (this takes a few minutes the first time) and puts a **Hallows' Eve Detector** shortcut on your Desktop.
4. Start the app with that shortcut.

## Updating

Click **Check for updates** at the bottom left of the app. When there's a newer version, click **Install and restart**. Your settings, logs and private server links are kept.

The first time, the app may say it doesn't know which version it is (a ZIP doesn't record that). Installing the newest version once fixes that.

## Using it

1. Pick the Roblox windows to watch, then **Start watching**. Windows can sit behind others, but they can't be minimised.
2. Each window gets a page with:
   - its live view;
   - switches for detection, Anti Disconnect and Auto rebirth;
   - its hotbar: click characters there to auto equip them;
   - a box for the private server link.
3. Double-click a window in the sidebar to rename it.

## Discord pings (optional)

Pings only work with a Discord bot token, which you never share. The app makes `discord_config.json` the first time it runs:

```json
{
  "bot_token": "",
  "channel": "event-ping",
  "ping_role": "ps-ping"
}
```

Put the bot's token in `bot_token`; the bot needs to be in the server with that channel and role. Without a token, everything else works and pings stay off.

## Files

| File | What it is |
|---|---|
| `hallows_eve_detector.py` | Watching, equipping, rebirths, Discord (also has a console mode: `--help`) |
| `detector_ui.py` | The app window (Qt / PySide6) |
| `updater.py` | Check for updates / install them from this repository |
| `Install.bat`, `create_shortcut.ps1` | First-time setup and shortcuts |

Your own data stays on your PC and is never uploaded: `discord_config.json`, `private_servers.json`, `window_names.json`, `rebirth_log.csv`, `ui_settings.json` and `detector_output.log`.
