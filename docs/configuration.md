# Configuration

Everything lives in `config.toml`. `python -m arm setup` writes one for you; [`config.example.toml`](../config.example.toml) shows every setting. After changing it, restart the program (or run the service installer again).

## Sections

| Section | Turns on | Notes |
|---|---|---|
| `[web]` | The web page | `port` (8080), `host` ("0.0.0.0" = whole home network, "127.0.0.1" = this computer only) |
| `[report]` | Night recording and the morning report | `at` ("08:00", local time), `keep_days` (30), `nights_dir`, `reports_dir` |
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
| `links` | 0 (auto) | Read-only connections held open at once: 0 picks 2 for an ASIAIR, 1 for PHD2 |
| `dialect` | "asiair" | "asiair" or "phd2" (PHD2, including PHD2 run by NINA) |
| `units` | "pixels" | What the rig reports guiding errors in: "arcsec" or "pixels" (see below) |
| `guide_scale` | 1.0 | Arcsec per guide-camera pixel, binning included. Only used with `units = "pixels"`. Or give `guide_pixel_um`, `guide_focal_mm` and `guide_binning` and it's worked out |
| `warn`, `alert` | 0.80, 1.00 | Total RMS (arcsec) above which the rig is in WARN / ALERT |
| `hysteresis` | 0.05 | How far below an edge the RMS must fall to leave a band, so it doesn't flicker |
| `persist_s` | 20 | How long a new RMS band must hold before it counts |
| `stopped_grace_s` | 20 | Guiding stopped or star lost longer than this → ALERT |
| `planned_stop_grace_s` | none | A clean stop by the plan or sequencer (goto, autofocus, flip) shows as "Paused by plan" and alerts only after this long. Suggested: 600 for ASIAIR, 300 for NINA |
| `offline_grace_s` | 30 | Rig unreachable longer than this while armed → ALERT |
| `frame_timeout_s` | 30 | Guiding but no guide frames for this long → ALERT. Use 60 for NINA |
| `task_limit_s` | 600 | A known task (calibration...) running longer than this → ALERT |
| `settle_fail_limit` | 3 | Failed settles in a row → ALERT |
| `settle_max_s` | 180 | Stop treating frames as settling after this long |
| `lat`, `lon` | none | Site position, degrees (north and east positive). See [End of the night](#end-of-the-night) |
| `dawn_sun_alt` | -18 | Sun altitude that counts as dawn |
| `filter_wheel` | none | For report labels: `true` if the plan changes filters, `false` for a one-shot colour camera |
| `sky_image_url` | none | An all-sky camera's latest image, shown in the rig's detail view. Needs Pillow (in requirements.txt) |
| `sky_every_s` | 120 | How often to fetch it (also fetched straight away on ALERT) |
| `sky_stretch` | true | Stretch the contrast so cloud shows on a dark night image |
| `screen_method`, `screen_window`, `screen_dithers` | "stdev", 100, true | The figure shown. See below |
| `alert_method`, `alert_window`, `alert_min_frames` | "stdev", 30, 10 | The figure the alert levels use: dithers and settling are always left out |
| `enabled` | true | `false` keeps a rig in the file without watching it |

### ASIAIR

- **Units:** ASIAIRs report RA/Dec errors in arcsec, at the guide scale the ASIAIR worked out. Use `units = "arcsec"`; no guide scale is needed. `setup` checks this from live frames (the ratio between the RA/Dec error and the camera-pixel error is the guide scale).
- **Telling ASIAIRs apart:** every ASIAIR calls itself "ASI AIR guider", so each rig is known by its address. Give each one a DHCP reservation on your router.
- **Plans:** an ASIAIR plan stops guiding for each goto, autofocus and meridian flip, while the guide camera keeps looping. With `planned_stop_grace_s = 600` these show as "Paused by plan" and only alert if guiding isn't back within 10 minutes.
- **End of a plan:** the plan stops the guide camera only when it has finished. The rig then disarms itself and shows "Plan finished".
- **Connections:** an ASIAIR drops each port 4400 connection about 16 s after it opens, and after an ASIAIR restart an old connection can go silently dead. The program reconnects at once and, by default, keeps two connections opened half a cycle apart, so one is always listening and no guide frames are missed (each line is used once). It also re-opens a link that has been silent for 90 s.

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

## Night reports

With `[report]`, every rig's guiding stream is recorded each night (`nights/<evening date>/<rig>.jsonl`, about 5 MB per rig per night), and after `at` the night is replayed through the same logic and written as a report page (`reports/guiding-night-<date>.html`), linked from the web page under *Night reports*. Both are deleted after `keep_days`.

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
