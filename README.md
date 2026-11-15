# PrintPocket

A phone app for your OctoPrint printers that you host yourself. No cloud account, no subscription and no AI.

PrintPocket is an OctoPrint plugin. It serves a small web app from your Pi that you add to your phone's home screen, where it opens full screen like a native app. It also sends push notifications straight from the Pi to your phone.

To reach it from outside your home, use [Tailscale](https://tailscale.com): your phone and your Pi join the same private network, so nothing is exposed to the internet and no relay server is needed.

## What it does

- **Live status.** Shows state, progress ring, elapsed time, time left, and the clock time the print will finish.
- **Webcam.** Shows a live MJPEG stream, or a snapshot every 5 seconds to save data on mobile. It respects the flip and rotate settings from OctoPrint.
- **Controls.** Pause, resume and cancel (cancel asks for confirmation). Tap a temperature to set it, use your OctoPrint preheat profiles, or cool down. It can also connect the printer if it's offline.
- **Files.** Search everything on OctoPrint, sorted newest first, with print time estimates and whether the last print of each file worked. Tap a file to print it.
- **Push notifications.** Each phone picks the events it wants:
  - print started, finished, failed or cancelled
  - paused
  - filament change (M600)
  - progress every N%
  - printer errors

  Tapping a notification opens the app.
- **Works offline.** The app shell is cached, so the icon still opens when the Pi is unreachable and tells you so.

Each Pi gets its own home screen icon, named after the printer. If you have three printers, you get three icons.

## How it works

```
 phone (home screen app)
   │  https over Tailscale (or http on your home Wi-Fi)
   ▼
 OctoPi ── OctoPrint ── PrintPocket plugin
   │                     ├─ serves the app at /plugin/printpocket/
   │                     ├─ webcam snapshot proxy
   │                     └─ Web Push ──► Apple / Google / Mozilla push service ──► phone
```

- The app uses OctoPrint's own REST API with an **application key**, which you approve once per device.
- Notifications use the standard **Web Push** protocol with a VAPID key the plugin generates on first start. Messages are end-to-end encrypted to your phone. The push services only relay ciphertext.
- Nothing is sent anywhere except those push messages.

## Install

### 1. Install the plugin on each Pi

In OctoPrint, open Settings, then Plugin Manager, then **Get More**. Under **...from URL**, paste:

```
https://github.com/CHANGE_ME/octoprint-printpocket/archive/main.zip
```

Click Install, then restart OctoPrint when it asks.

You can also install it over SSH on OctoPi:

```bash
~/oprint/bin/pip install https://github.com/CHANGE_ME/octoprint-printpocket/archive/main.zip
sudo service octoprint restart
```

### 2. Put the Pi on Tailscale with HTTPS

Skip any steps you've already done on this Pi.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg 80
```

`tailscale serve` gives the Pi a real HTTPS address like `https://octopi.tail1234.ts.net` with a valid certificate, and forwards it to OctoPrint. If it says HTTPS is off for your tailnet, enable **MagicDNS** and **HTTPS Certificates** on the DNS page of the Tailscale admin console, then run the command again.

Run `tailscale serve status` to see the address.

HTTPS matters because phones only allow push notifications and installable apps on secure addresses. Plain `http://octopi.local/plugin/printpocket/` still works for status and controls on your home Wi-Fi.

Install the Tailscale app on your phone and sign in with the same account.

### 3. Add it to your phone

1. On your phone, open `https://<pi-name>.<tailnet>.ts.net/plugin/printpocket/`.
2. **iPhone:** in Safari, tap Share, then **Add to Home Screen**. **Android:** in Chrome, open the menu, then **Add to Home screen** (or **Install app**).
3. Open PrintPocket from the new icon and tap **Connect**.
4. Approve the request:
   - **On a computer:** a popup appears on any open OctoPrint page. Click Allow.
   - **On the phone:** tap **Approve here** and sign in to OctoPrint.
   - **With a key:** in OctoPrint, go to Settings, then Application Keys, and generate a key for "PrintPocket". Paste it into the app under **Use an API key instead**.
5. Go to the **Alerts** tab, tap **Turn on notifications**, then **Send a test**.

On iPhone, notifications need iOS 16.4 or later, and only work from the home screen icon, not a Safari tab.

## Settings

In OctoPrint, go to Settings, then PrintPocket:

| Setting | Default | What it does |
|---|---|---|
| Printer name | OctoPrint's instance name | Used in notification titles and the home screen icon label |
| Progress updates every | 25% | Step size for progress notifications. Each phone still decides whether it wants them. Set to 0 to turn them off. |
| Push contact | `mailto:printpocket@users.noreply.github.com` | The contact address included in push requests, as the Web Push standard requires. Put your own address here if you like. |

## Troubleshooting

- **No webcam in the app.** PrintPocket uses the webcams OctoPrint knows about. Check Settings, then Webcam & Timelapse. On https, a stream URL that starts with `http://` is blocked by the browser, so the app switches to snapshots. The default OctoPi stream URL `/webcam/?action=stream` works fine.
- **Test notification never arrives.** Check `~/.octoprint/logs/octoprint.log` for lines starting with `Push to`. The Pi needs outbound internet access to reach the push services.
- **The app says the key no longer works.** The key was deleted in OctoPrint (Application Keys). Connect again.
- **Remove a phone.** In the app, open Alerts and tap Turn off. You can also delete it from `~/.octoprint/data/printpocket/subscriptions.json`.

## Development

```bash
python -m venv venv && . venv/bin/activate
pip install octoprint -e . pytest
pytest
octoprint serve --basedir ./devbase   # enable the Virtual Printer plugin, then connect to VIRTUAL
```

The web app is plain HTML, CSS and JS in `octoprint_printpocket/static/app/`, with no build step. Edit the files and reload.

## License

AGPLv3, the same as OctoPrint.
