# Hallows' Eve Detector

A Windows app that watches your Roblox windows for the **Hallows' Eve** event and helps out while it's on:

- **Detects the event** in each window (even behind other windows), with a countdown.
- **Auto equip:** holds the characters you pick during the event, in the order you picked them, skipping any that already have Dracula, and unequips them when it ends.
- **Anti Disconnect:** reconnects after a disconnect, closes the "Welcome Back" popup and clicks CLAIM on the "Daily Rewards" window.
- **Auto rebirth:** rebirths whenever your cash covers the next rebirth, with a log.
- **Trim Roblox RAM:** frees memory your Roblox windows aren't using, every 2 minutes (on by default).
- **Discord ping** (optional) with each window's private server link. Switch it off per window to detect there without pinging.
- **Account launcher:** save your Roblox accounts and launch each one straight into its private server, as many Roblox windows at once as you like.
- **Follow event** (optional): when one private server gets the event, your other accounts' windows move there and go back when it ends.

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
   - switches for detection, Anti Disconnect, Auto rebirth and **Discord ping** (turn the ping off to keep detecting in that window without pinging; it's remembered by window name, so an account's window keeps it across launches);
   - its hotbar: click characters there to auto equip them;
   - a box for the private server link.
3. Double-click a window in the sidebar to rename it.

## Accounts and launching Roblox

Click **Open** next to **Accounts** in the sidebar.

1. Add an account:
   - **Sign in with browser** opens the Roblox login page in a small Edge window. Sign in as usual; the window closes by itself and the account is saved. Repeat for each account.
   - **Add with cookie** lets you paste an account's `.ROBLOSECURITY` cookie instead.
2. Save your private servers: click **Private servers**, give each one a name and paste its link (`roblox.com/share?code=…` or a `…?privateServerLinkCode=…` link), then **Add**. Rename a server by clicking its name; the bin removes it.
3. Click an account's server box and pick the server it should join. Switch it the same way whenever you like. If that account's Roblox window is already open in another server, a **Relaunch** button appears: it closes the old window and opens the account in the new server.
4. Click **Launch**, or tick several accounts and click **Launch selected**, or **Launch all**. Roblox opens signed in to that account and joins the server. Several accounts start at the same time, and accounts already open are skipped, as a second window would kick the first out of the game.

**Relaunch previous session** opens the windows you were last watching again: each account in the server it was in, with the same window name, switches and hotbar slots, plus the sidebar's Discord and Trim RAM switches. Windows still open aren't reopened, they just get their settings back. Only saved accounts can be reopened. Once every window has closed, the next ones you open start a new record.

Every launch opens its own Roblox window: a small background process holds Roblox's one-window lock, so any number of Roblox windows run side by side, launched here or opened yourself, and they stay open when you close the app. It goes away by itself once the app and all Roblox windows are closed. If a Roblox was already open before the app started, the lock is left with it, because taking it would close that window: close Roblox and launch from the app to run several. The new window is named after the account and gets its private server link for the Discord ping. With the account's **Start watching** switch on (the default), the app starts watching it straight away.

Sign-ins are saved in `accounts.json`, encrypted by Windows for your Windows user only, so the file is useless on any other PC. **Never share your cookie**: anyone who has it is signed in as you.

## Master switch

The **Detector on/off** switch under the picture at the top left stops everything the app does on its own: detection, Discord pings, equipping, auto rebirth, Anti Disconnect, Trim RAM and Follow event. Your Roblox windows stay open, and you can still launch accounts by hand. Switching it back on carries on with every switch as it was. The app remembers the setting, so it's still off the next time you open it.

## Follow event (optional)

Switch on **Follow event** in the sidebar (it's off by default, and the app remembers your choice). When a watched window shows the event, every saved account with characters selected and a Roblox window open in another private server is closed and reopened in the server with the event, all at the same time. Each window gets about a second to close by itself before it's ended, so no time is lost waiting. The windows keep their names and switches, so they equip your chosen characters as usual. When the event ends, each account goes back to the server it was in. Its saved server doesn't change.

- The window with the event needs a private server link: launch it from Accounts or paste the link on its page.
- Only saved accounts move, and only ones with a window open and characters selected in **Choose slots**. Accounts already in a server with the event stay there.
- It doesn't move anyone if less than a minute of the event is left, or for a test event.
- The moved windows don't ping Discord; the window that found the event already did.
- The app has to stay open: if you close it during an event, the accounts stay where they are.

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
| `accounts.py` | Saved accounts, browser sign-in, launching Roblox into a server |
| `updater.py` | Check for updates / install them from this repository |
| `build_exe.ps1`, `.github/workflows/build-exe.yml` | Build the .exe; GitHub does this on every push and publishes it as a release |
| `Install.bat`, `create_shortcut.ps1` | Setup for running from source |

When a Roblox window closes, the app notes why in its activity feed and in `window_closes.csv`, using Roblox's own logs: it froze, the server disconnected it, it was closed normally, this app closed it, or no reason was given.

Your own data stays on your PC and is never uploaded: `discord_config.json`, `private_servers.json`, `window_names.json`, `rebirth_log.csv`, `window_closes.csv`, `previous_session.json`, `ui_settings.json`, `detector_output.log`, `accounts.json` and `avatars/`.
