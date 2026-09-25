# ⚙️ MARK L (LI) — J.A.R.V.I.S.

### A Real-Time Voice AI Assistant with a Live 3D Geospatial Command Center

A real-time voice AI that hears, sees, understands, and controls your computer — on any OS. Built on the Gemini Live API for native audio streaming, with a hand-built PyQt6 holographic HUD and its own companion 3D-globe intelligence viewer, **God's Eye View**. Zero subscriptions, everything runs locally against your own API key.

---

## ✨ Overview

MARK L is where the assistant stops being a tool and starts being a presence. It remembers yesterday's conversation, watches the topics you care about, speaks first when it has something worth saying, and now has eyes on the whole planet: a live 3D globe it can fly, zoom, and populate with real aircraft, satellites, and CCTV feeds — all driven entirely by voice.

It's not just an assistant — it's an extension of your digital life, with a command deck to match.

---

## 🚀 Capabilities

### Core Assistant
| Feature | Description |
|---|---|
| 🎙️ Real-time Voice | Ultra-low latency conversation in any language via the Gemini Live API |
| 🧩 Tool-Calling Core | ~30 real tools registered with Gemini — every capability below is something the model actually decides to invoke mid-conversation, not a canned command list |
| 👁️ Visual Awareness | Real-time screen capture and webcam vision piped straight into the live voice session |
| 🧠 Persistent Memory | Remembers projects, preferences, and personal context across sessions |
| 🗓️ Session Memory | Summarises each conversation and mentions it naturally next morning — consumed after use, never repeats |
| 🔔 Proactive Engine | Time-aware, context-aware check-ins that rotate focus so they never open the same way twice |
| 👁️‍🗨️ Background Monitoring | User-configured topic watching — checks once a day, alerts naturally, crypto/finance topics hard-blocked |
| 📊 Hardware Monitoring | Continuous CPU/RAM/GPU/temperature telemetry with voice alerts on sustained spikes |
| ⌨️ Hybrid Input | Seamlessly switch between typing and voice |
| 🌅 Morning Briefing | Greets you, recaps yesterday, reads live news and your unread email digest — in parallel, no dead air |

### Productivity & Life Integration
| Feature | Description |
|---|---|
| 🗓️ Calendar Control | Create, list, and delete events in the native macOS Calendar app — works with any synced calendar |
| ☑️ Task Management | Full Reminders-app integration — a real, persistent to-do list, not a one-off alarm |
| ✉️ Gmail Integration | List, search, read, and send email via the Gmail API, plus an unread digest in the morning briefing |
| 🗒️ Obsidian Vault Control | Search, read, create, and append notes — including auto-generated Daily, Literature, and Meeting Notes that match your vault's real templates |
| 🔥 Habit Tracking | Voice-logged habits with streak tracking, mirrored into today's Obsidian daily note |
| ⏲️ Focus / Pomodoro Sessions | Start a focus block; JARVIS stays quiet and announces completion when it ends |
| 🤝 Meeting Prep | One command ties Calendar + Gmail + Obsidian together — finds the meeting, pulls related email, and pre-fills a Meeting Notes page |
| ⏰ Smart Reminders | OS-native scheduled notifications (Task Scheduler / launchd / systemd) |
| ⭐ Assistant Customization | Change the assistant's name, your name, and the UI accent color — live, from the HUD |

### System & Web
| Feature | Description |
|---|---|
| 🖥️ System Control | Volume, brightness, WiFi, window snapping, power — all by voice |
| 🖱️ Desktop Control | Wallpaper, desktop organization, and an AI-generated-code fallback (sandboxed) for anything not hardcoded |
| 🌐 Browser Control | Real native-browser navigation for simple requests, full Playwright automation for anything interactive |
| 🖲️ Vision-Guided Clicking | Describe a UI element in plain language and JARVIS finds and clicks it, screenshot + Gemini vision under the hood |
| 🔍 Multi-Backend Web Search | `news` / `research` / `price` / `compare` / `search` — Gemini, DuckDuckGo, and (if configured) Google CSE / Tavily raced in parallel, first valid answer wins |
| ✈️ Flight Finder | Scrapes live Google Flights results and has Gemini parse them into a real itinerary |
| 🎮 Game Updater | Checks and triggers updates on Steam and Epic Games |
| 📂 File Processor | Read, summarize, transcribe, and convert images, PDFs, audio, video, spreadsheets, and more |
| 💻 Code Helper & Dev Agent | Inline code review/fix/explain on one file, or hand off an entire multi-file project to be planned, written, installed, run, and self-corrected on error |
| 📨 Send Message | Compose and send through WhatsApp, Telegram, Signal, Discord, Instagram, Messenger |
| 🎬 YouTube Control | Search, play, summarize, and pull trending videos |
| 📱 Remote Dashboard | Control the assistant from your phone over your local network via QR pairing |
| 📋 Clipboard Intelligence | Copy any text → floating panel with Translate / Summarize / Explain / Fix |
| ⚡ Auto-Start on Boot | Registers with the OS's own startup system |

---

## 🛰️ God's Eye View — Voice-Controlled Live 3D Earth

The headline feature of this build: a companion **Cesium-based 3D globe application** that JARVIS launches, embeds directly inside its own window, and fully controls by voice through a local HTTP bridge — no separate app to babysit, no manual clicking required.

Ask, and JARVIS will:
- **Fly anywhere** — a named city, a landmark, or raw coordinates, with automatic whole-country/whole-city or close-landmark framing
- **Track real aircraft** — enable live Flights or Military Flights layers and auto-select/follow the nearest airborne contact
- **Predict the ISS's next pass** over any location
- **Pull up CCTV feeds, radio, and a cockpit view** for the current target
- **Annotate the map**, adjust visual style and post-processing, switch map layers/imagery stacks, and answer open-ended analyst questions about what's on screen
- **Report back its own view state** — location, zoom, active layers — so follow-up commands stay context-aware

Twenty-seven distinct `gev_*` tools are merged directly into Gemini's tool schema alongside JARVIS's core ~30, so the model reasons about geospatial actions exactly the same way it reasons about opening an app or checking the calendar.

> God's Eye View is a Vite/Cesium project living alongside this repo (default: `../gods-eye-view`). `npm run dev` will install and start it automatically the first time; JARVIS works perfectly well without it, and simply reports the feature as unavailable if the folder isn't found.

---

## 🖥️ The HUD

A hand-painted, sci-fi holographic interface (PyQt6, no game engine) — not a wrapper around a chat window:

- **Animated avatar orb** that reflects listening/thinking/speaking state in real time
- **Always-visible live panels** — open Reminders, upcoming Calendar events, and a rotating, photorealistically textured Earth globe with a glowing marker at your real location
- **Global Ops overlay** — a full-screen broadcast-style dashboard (expand the globe panel) with live CPU/RAM/GPU/temperature gauges, a mini world map, and real weather at your location
- **Feature-activity tiles** — a live grid tracking which tool categories have fired, with counts and last-used state, so you can see at a glance what JARVIS has actually been doing
- **God's Eye mode** — a dedicated tab that embeds the 3D globe inline (Ctrl+G), falling back to a one-click browser launch if the optional Chromium view isn't installed

---

## ⚡ Quick Start

**Recommended — one command, handles everything:**
```bash
git clone https://github.com/JoshuaKhooba/JarvisAI.git
cd JarvisAI
npm run dev
```
First run creates the Python virtual environment, installs requirements, and (if `../gods-eye-view` exists) installs and starts God's Eye View alongside JARVIS. Use `npm run jarvis` to skip GEV, or `npm run setup` to just prepare the environment without launching anything.

**Manual (no Node.js required):**
```bash
git clone https://github.com/JoshuaKhooba/JarvisAI.git
cd JarvisAI
python3 setup.py
python3 main.py
```

> ⚠️ **Installation Note:** Some OS-specific dependencies aren't bundled in `requirements.txt` to keep the repo lightweight. If you hit a `ModuleNotFoundError`, install the missing package with `pip install <module_name>`.

---

## 📋 Requirements

| Requirement | Details |
| --- | --- |
| **OS** | Windows 10/11, macOS, or Linux |
| **Python** | **3.11 or newer** — required for `asyncio.TaskGroup` / exception groups used in the live audio pipeline |
| **Node.js** | 24+ — only needed for the `npm run dev` launcher and God's Eye View |
| **Microphone** | Required for voice interaction |
| **API Key** | Free Gemini API key, saved to `config/api_keys.json` |
| **Optional integrations** | Gmail OAuth credentials, an Obsidian vault path, and/or a manual lat/lon override — each degrades gracefully to "not configured" if skipped |

---

## 🗂️ Project Structure

```
JarvisAI/
├── main.py                    # Core loop — Gemini Live session, audio I/O, tool dispatch
├── ui.py                      # PyQt6 HUD — avatar orb, live panels, Earth globe, Global Ops, God's Eye embed
├── setup.py                   # First-run dependency installer
├── package.json / scripts/    # npm launcher — venv + deps + optional God's Eye View startup
├── actions/
│   ├── gods_eye.py            # God's Eye View bridge — launches GEV, relays gev_* voice commands
│   ├── gods_eye_tools.json    # 27 gev_* tool schemas, merged into Gemini's tool list at startup
│   ├── obsidian_control.py    # Vault search/read/create/append, template-matched note generation
│   ├── calendar_control.py    # Native macOS Calendar (AppleScript)
│   ├── task_manager.py        # Native macOS Reminders — persistent to-do list
│   ├── email_control.py       # Gmail API — read/search/send/digest
│   ├── habit_tracker.py       # Streak-tracked habits, mirrored to Obsidian daily notes
│   ├── focus_timer.py         # Pomodoro/focus session state + completion announcer
│   ├── meeting_prep.py        # Composes calendar_control + email_control + obsidian_control
│   ├── location.py            # Manual override or IP geolocation for the globe/weather
│   ├── weather.py             # Open-Meteo lookup for the HUD's live weather readout
│   ├── weather_report.py      # Voice "what's the weather" tool (separate from weather.py)
│   ├── web_search.py          # Multi-backend racing search (Gemini / DDG / Google CSE / Tavily)
│   ├── screen_processor.py    # Screen & webcam capture for vision
│   ├── background_monitor.py  # Daily topic watching — crypto/finance hard-blocked
│   ├── proactive.py           # Time/context/rotation-aware unprompted check-ins
│   ├── reminder.py            # OS-native one-off scheduled notifications
│   ├── system_monitor.py      # CPU / RAM / GPU / temperature telemetry + alerts
│   ├── computer_settings.py   # Volume, brightness, WiFi, window snapping, power
│   ├── computer_control.py    # Low-level mouse/keyboard + vision-guided clicking
│   ├── browser_control.py     # Native navigation + Playwright automation
│   ├── file_controller.py     # Sandboxed file/folder operations
│   ├── file_processor.py      # Document/image/audio/video processing
│   ├── send_message.py        # WhatsApp / Telegram / Signal / Discord / Instagram / Messenger
│   ├── flight_finder.py       # Google Flights scrape + Gemini parsing
│   ├── youtube_video.py       # Search / play / summarize / trending
│   ├── game_updater.py        # Steam / Epic update automation
│   ├── code_helper.py         # Single-file code write/edit/explain/run/optimize
│   ├── dev_agent.py           # Full multi-file project build-and-self-fix agent
│   ├── open_app.py            # Cross-platform application launcher
│   └── desktop.py             # Wallpaper, desktop organization, sandboxed AI codegen fallback
├── dashboard/
│   └── server.py               # Local HTTP + WebSocket server for the phone Remote Dashboard
├── memory/
│   ├── memory_manager.py      # Load/save long_term.json — identity, preferences, sessions, monitors
│   └── long_term.json         # Persistent store (gitignored — personal data)
├── core/
│   └── prompt.txt              # Assistant personality and tool-routing rules
└── config/
    └── api_keys.json           # API key, assistant/user name, UI color, vault path, location override
```

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.

---

## 👤 Author

Built by **Joshua Khooba**.
