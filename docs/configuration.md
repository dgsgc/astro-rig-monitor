# Configuration

Everything lives in `config.toml`. The setup page (`http://localhost:8080/setup`, opened automatically the first time) or `python -m arm setup` writes one for you; [`config.example.toml`](../config.example.toml) shows every setting. After changing it, restart the program (or run the service installer again).

## Sections

| Section | Turns on | Notes |
|---|---|---|
| `[web]` | The web page | `port` (8080), `host` ("0.0.0.0" = whole home network, "127.0.0.1" = this computer only), `allow_origin` (off; `"*"` lets your own page elsewhere read the data, see [build-a-display.md](build-a-display.md#2-your-own-web-page)), `setup_from_lan` (false: the setup page at `/setup` only works on the monitor's own computer; true lets any computer on your network change the settings) |
| `[report]` | Night recording and the morning report | `at` ("08:00", local time), `keep_days` (30), `nights_dir`, `reports_dir`, `archive_dir` (off; see [Night reports](#night-reports)) |
| `[notify]` | Phone / chat alerts | See [Notifications](#notifications) |
| `[mqtt]` | Publishing to an MQTT broker | `host`, `port`, `username`, `password`, `tls`, `ca_file`, `prefix` ("arm": every topic starts with it). See [mqtt-spec.md](mqtt-spec.md) |
| `[defaults]` | Settings shared by every rig | Any rig setting below |
| `[rigs.<id>]` | One rig | `<id>`: lowercase letters, digits and `_` |

A section is on when it's present: delete or comment out its `[...]` line to turn it off. Top-level settings: `bridge_id` ("main"; only matters with MQTT) and `log_file`.

## Rig settings

| Setting | Default | Meaning |
|---|---|---|
| `name` | the id | Shown on the page and in alerts |
| `host`, `port` | –, 4400 | Where the ASIAIR or PHD2 is |
| `app_port` | none | ASIAIR only, optional: also listen to the ASIAIR app's own port (4700) for autofocus, filter changes and plan progress. See [The ASIAIR app port](#the-asiair-app-port-optional) |
| `links` | 0 (auto) | Read-only connections held open at once: 0 picks 2 for an ASIAIR, 1 for PHD2 |
| `dialect` | "asiair" | "asiair", "phd2" (PHD2, including PHD2 run by NINA), or "tracker" (a tracker without guiding; see [Trackers without guiding](#trackers-without-guiding)) |
| `units` | "pixels" | What the rig reports guiding errors in: "arcsec" or "pixels" (see below) |
| `guide_scale` | 1.0 | Arcsec per guide-camera pixel, binning included. Only used with `units = "pixels"`. Or give `guide_pixel_um`, `guide_focal_mm` and `guide_binning` and it's worked out |
| `warn`, `alert` | 0.80, 1.00 | Total RMS (arcsec) above which the rig is in WARN / ALERT |
| `hysteresis` | 0.05 | How far below an edge the RMS must fall to leave a band, so it doesn't flicker |
| `persist_s` | 20 | How long a new RMS band must hold before it counts |
| `stopped_grace_s` | 20 | Guiding stopped or star lost longer than this → ALERT |
| `planned_stop_grace_s` | none | A clean stop by the plan or sequencer (goto, autofocus, flip) shows as "Paused by plan" and alerts only after this long. Suggested: 600 for ASIAIR, 300 for NINA |
| `offline_grace_s` | 30 | Rig unreachable longer than this while armed → ALERT |
| `frame_timeout_s` | 30 | Guiding but no guide frames for this long → ALERT. Use 60 for NINA |
| `task_limit_s` | 600 | A known task (calibration, or with `app_port` an autofocus or filter change) running longer than this → ALERT |
| `settle_fail_limit` | 3 | Dithers in a row whose settle failed → ALERT. NINA settles again after a timed-out settle; that retry counts as the same dither |
| `settle_clear_s` | 120 | Guiding within the levels this long after a failed settle clears that ALERT (otherwise it would last until the next dither settles) |
| `settle_max_s` | 180 | Stop treating frames as settling after this long |
| `lat`, `lon` | none | Site position, degrees (north and east positive). See [End of the night](#end-of-the-night) |
| `dawn_sun_alt` | -18 | Sun altitude that counts as dawn |
| `filter_wheel` | none | For report labels: `true` if the plan changes filters, `false` for a one-shot colour camera |
| `sky_image_url` | none | An all-sky camera's latest image, shown in the rig's detail view. Needs Pillow (in requirements.txt). The setup page can check it. For Dropbox use a share link ending `raw=1` (the setup page changes `dl=0` for you) |
| `sky_every_s` | 120 | How often to fetch it (also fetched straight away on ALERT) |
| `sky_stretch` | true | Brighten a dark night image so cloud shows (at most 5×, colours kept) |
| `screen_method`, `screen_window`, `screen_dithers` | "stdev", 100, true | The figure shown. See below |
| `alert_method`, `alert_window`, `alert_min_frames` | "stdev", 30, 10 | The figure the alert levels use: dithers and settling are always left out |
| `enabled` | true | `false` keeps a rig in the file without watching it |

### ASIAIR

- **Units:** ASIAIRs report RA/Dec errors in arcsec, at the guide scale the ASIAIR worked out. Use `units = "arcsec"`; no guide scale is needed. `setup` checks this from live frames (the ratio between the RA/Dec error and the camera-pixel error is the guide scale).
- **Telling ASIAIRs apart:** every ASIAIR calls itself "ASI AIR guider", so each rig is known by its address. Give each one a DHCP reservation on your router.
- **Plans:** an ASIAIR plan stops guiding for each goto, autofocus and meridian flip, while the guide camera keeps looping. With `planned_stop_grace_s = 600` these show as "Paused by plan" and only alert if guiding isn't back within 10 minutes.
- **End of a plan:** the plan stops the guide camera only when it has finished. The rig then disarms itself and shows "Plan finished".
- **Connections:** an ASIAIR drops each port 4400 connection about 16 s after it opens, and after an ASIAIR restart an old connection can go silently dead. The program reconnects at once and, by default, keeps two connections opened half a cycle apart, so one is always listening and no guide frames are missed (each line is used once). It also re-opens a link that has been silent for 90 s.

### The ASIAIR app port (optional)

Port 4400 tells the monitor how guiding is going, but not *why* a plan paused it. With
`app_port = 4700` it also listens to the port the ASIAIR app itself uses, which announces what
the plan is doing. Then:

- pauses get their real name: "AF: new filter", "AF: timer", "AF: temp", "Filter change", instead of "Paused by plan";
- an autofocus or filter change that hasn't finished after `task_limit_s` alerts as stuck;
- the web page shows the target, sub *n* of *N*, filter, exposure and the last autofocus result;
- the ASIAIR's own under-voltage, overheating or over-current warning shows as a WARN;
- the night report names each pause ("Filter change O→S and autofocus", "Autofocus (timer)"), gives the focus HFR, and counts the subs taken.

It stays read-only: the monitor never sends anything on this port either. The ASIAIR drops a
connection that sends nothing about every 16 s, so, as on port 4400, two staggered links are
kept open and nothing is missed.

> **Unofficial, so it could change.** Port 4700 is the ASIAIR app's private interface, not a
> published one. It was worked out from ASIAIR firmware 14.39 (November 2026). ZWO has kept it
> stable for years and a change seems unlikely, but a future firmware could rename events,
> change their fields or close the port. If that happens the monitor carries on as without
> `app_port`: guiding, levels and alerts all come from port 4400, odd lines on 4700 are
> counted and ignored, and pauses fall back to "Paused by plan". The firmware it saw is shown
> as `app.firmware` in the rig's state. Please open an issue if a firmware update breaks it.

### Trackers without guiding

A camera on a star tracker with no guiding, run by a small controller that takes the frames, dithers between them and measures each frame's stars, can be watched too (`dialect = "tracker"`). There's no guide graph or RMS: the page shows the frame count, the star size and how many stars were found, and the detail view graphs both across the run. The monitor reads the controller's status page every `poll_s` seconds (an HTTP GET; nothing is ever sent to the rig).

| Setting | Default | Meaning |
|---|---|---|
| `host` or `status_url` | – | The controller; the status page is `http://<host>/status` unless `status_url` says otherwise |
| `poll_s` | 10 | How often the status page is read |
| `cloud_drop` | 0.5 | Stars found below this fraction of the run's start → WARN "Cloud?" |
| `focus_warn_pct` | 30 | Stars this much larger (%) than at the run's start → WARN "Focus drifting" |
| `battery_warn`, `battery_alert` | 20, 10 | Camera battery (%) → WARN / ALERT |
| `persist_s`, `offline_grace_s`, `lat`, `lon`, `sky_image_url` | as above | Work the same as for guided rigs |

It alerts when the run stops with an error, when the controller can't be reached while a run is going, and on a low camera battery; it warns on cloud, focus drift, and a card with fewer shots left than frames left in the run. A run starting arms the rig; the run finishing (or being stopped) disarms it. The night report gives each tracker its own section: time imaging, frames taken, star size from the start to the end of each run, stars found, time lost to cloud, refocuses, and a chart of star size and stars found per frame.

The status page is JSON. The fields it uses are listed at the top of [`arm/tracker.py`](../arm/tracker.py): `state` (e.g. "Exposing", "Dithering", "Done", "Error"), `frame` and `total`, `remaining_s`, `camera` (`batterylevel`, `availableshots`), and `frame_focus` with each measured frame's star size (`history`) and star count (`counts`) and the run's starting `baseline` and `baseline_stars`. Any controller that serves these can be watched.

```toml
[rigs.tracker]
name = "Tracker"
dialect = "tracker"
host = "tracker.local"
```

### PHD2 and NINA

- **Units:** PHD2 reports pixels, so set `units = "pixels"` and the guide scale (PHD2: *Guiding Assistant* or the guide log; NINA: the *Guider* panel).
- **NINA** stops guiding cleanly for each autofocus, flip and target change, then restarts it about 2 minutes later. Use `planned_stop_grace_s = 300`: those show as "Paused by NINA". Star lost (cloud) still alerts after `stopped_grace_s`.
- **Image downloads:** PHD2 can pause 20–30 s while NINA downloads each image, so use `frame_timeout_s = 60`.
- **Dithers:** NINA dithers can be large (20 px or more). `screen_dithers = false` keeps them out of the figure shown.
- **Remote observatories:** reach the PC over Tailscale (or another VPN) and use its VPN address as `host`. PHD2 accepts connections from other computers on port 4400; Windows Firewall may need a rule for it, limited to the VPN's addresses.

## Matching the numbers you see

PHD2 and ASIAIR show RMS as the **standard deviation** about the average position, over the frames on the guide graph, dithers included. NINA's guider panel shows RMS **about zero**. The figure on the page is controlled by:

- `screen_method`: "stdev" (PHD2 / ASIAIR) or "rms" (NINA).
- `screen_window`: frames; set it to your guide graph's length (for example 50).
- `screen_dithers`: `true` includes dither and settle frames, as ASIAIR does.

The one thing that can't be matched: pressing *Clear* on the ASIAIR graph isn't broadcast, so the figure here keeps its history.

The alert levels always use a separate figure (`alert_method`, `alert_window` frames) with dithers and settling left out, so dithering never triggers an alert.

## End of the night

With `lat` and `lon` (town-level is plenty), a rig whose guiding has stopped (or that has gone offline) while the sun is rising past `dawn_sun_alt` disarms itself and shows "Finished at dawn", instead of alerting all morning. Still guiding at dawn means it keeps watching. Near midsummer at high latitudes the sun never gets to -18°, so on those nights dawn counts from 4° above the night's lowest sun.

Rigs re-arm themselves when guiding next starts.

## Notifications

```toml
[notify]
level = "alert"          # "alert", or "warn" to hear about WARN too
clear = true             # also say when a rig is back to normal, disarmed or finished
repeat_min = 0           # remind every N minutes while a rig stays in ALERT; 0 = never
ntfy_url = "https://ntfy.sh/arm-a-long-random-name"
# ntfy_token = "tk_..."  # for a protected topic or your own ntfy server
# discord_webhook = "https://discord.com/api/webhooks/..."
```

- **ntfy:** install the ntfy app (iOS, Android) and subscribe to the topic name, the last part of `ntfy_url`. Anyone who knows the name can read it, so make it long and random (`setup` does). You can also run your own ntfy server.
- **Discord:** in the channel, *Edit Channel → Integrations → Webhooks → New Webhook*, then *Copy Webhook URL*.
- **When it sends:** when a rig rises into the notify level, when an ALERT changes cause (say from RMS high to star lost), and with `clear`, when it's over. The 20 s persistence and grace periods have already filtered out single bad frames.
- **Test:** `python -m arm test-notify`.

## Web page

- **Open it** at `http://<computer>:8080` on the home network. From elsewhere, use a VPN such as Tailscale; don't forward a router port to it.
- **It can't change anything:** no arm/disarm or other commands, so a stray tap can't silence a rig.
- **It notices stale data itself:** if it loses the program, it shows a red banner, and rigs that were armed show as alerts.
- **Night** turns the page red. **Keep on** stops a phone or tablet sleeping (in browsers that support it). On iOS, *Share → Add to Home Screen* opens it full screen like an app.
- Up to 20 viewers at once, a few KB/s each.
- **Your own display:** the page's data is also at `/events` (live) and `/state.json` (a snapshot) for a page, desktop window or script of your own. See [Build your own display](build-a-display.md).

## Night reports

With `[report]`, every rig's guiding stream is recorded each night (`nights/<evening date>/<rig>.jsonl`, about 5 MB per rig per night), and after `at` the night is replayed through the same logic and written as a report page (`reports/guiding-night-<date>.html`), linked from the web page under *Night reports*. Both are deleted after `keep_days`. To keep the recordings for longer (to compare months of guiding, say), set `archive_dir` to a folder that's backed up: after each night ends (12:30 the next day) its recordings are copied there gzipped, about a tenth of the size, and never deleted. Unzip one with `gunzip` to replay it or build a report from it.

The report shows, per rig: headline figures, the night as a chart (RMS each minute against your levels, pauses, alerts, dithers, star-lost events and single-frame jumps), a list of what happened, and an hour-by-hour table.

**Why the plan paused** is worked out from what port 4400 reports, since neither ASIAIR nor PHD2 broadcasts autofocus or filter changes there:

| Label | Worked out from |
|---|---|
| Meridian flip | A slew with the target within half an hour of the meridian (needs `lat`/`lon`) |
| New target | A slew of 1° or more |
| Centring | A smaller slew |
| Recalibration | Added when it calibrated |
| Cloud | Three or more star-lost frames with no slew |
| Autofocus | Anything else; "autofocus or filter change" with `filter_wheel = true` |
| Paused by NINA | PHD2 rigs: PHD2 doesn't report slews |

By hand, from any recording or logger file:

```bash
python tools/night_report.py --config config.toml --rig refractor=nights/2026-01-14/refractor.jsonl --out night.html
```
