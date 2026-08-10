# 🏰 AubergeRP

[![CI](https://github.com/aubergeRP/aubergeRP/actions/workflows/ci.yml/badge.svg)](https://github.com/aubergeRP/aubergeRP/actions/workflows/ci.yml)
![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)

**The cozy, distraction-free roleplay engine.** *Stop configuring, start roleplaying.*

AubergeRP is a lightweight, self-hostable alternative to SillyTavern. It’s designed for those who want a beautiful, "plug-and-play" experience with local or remote LLMs, featuring native AI image generation without the headache of complex extensions.

The Docker setup ships with a bundled [LocalAI](https://localai.io/) instance — text and image models are **downloaded automatically** on first run, so you get a fully working text + image stack with a single command and no manual model management.

## ✨ Why AubergeRP?

| Feature            | AubergeRP          | Other Tools (ST, etc.)      |
| :---               | :---               | :---                        |
| **Setup Time**     | < 10 minutes       | Can take hours              |
| **Interface**      | Minimalist & Cozy  | Complex "Control Room"      |
| **Image Gen**      | Native & Automatic | Requires complex extensions |
| **Learning Curve** | None (Plug & Play) | High (Many sliders/tabs)    |

## 📸 Preview

| Desktop View | Mobile View |
| :---         | :--- |
| ![Desktop Screenshot](docs/img/desktop-main.png) | ![Mobile Screenshot](docs/img/mobile-view.png) |


## 🚀 Key Features

* **Zero-Friction Setup:** Get running in minutes with Docker.
* **Universal Connectivity:** Support for any OpenAI-compatible API (Ollama, OpenRouter, **but also local setup!** vLLM, ollama, etc.).
* **SillyTavern Compatible:** Seamlessly import and export your favorite `.png` or `.json` character cards.
* **Smart Image Generation:** The AI triggers image generation automatically based on the story context (via ComfyUI or SD-WebUI).
* **Lightweight Stack:** No complex build steps. Just Python (FastAPI) and Vanilla JS.
* **Telegram Bots:** Turn any character into a Telegram bot (long-polling or webhook), with an optional **dialogue-only** style that drops narration for a natural instant-messaging feel.
* **Proactive Characters:** Characters can message you first — on a schedule defined in the character card (daily, weekly, interval, one-shot) or when the Proactive Behavior Engine decides a message is natural.
* **Timezone Aware:** Each user (web session or Telegram chat) has their own IANA timezone, so "good morning" arrives in the morning.
* **Admin Dashboard:** Easily manage your connectors, characters, and check your usage stats.
* **Operations Dashboard:** See at a glance whether bots, LLM calls, proactive schedules and summarization are healthy — plus an optional Prometheus `/metrics` endpoint.


## 🛠 Quick Start

1. **Clone & Config**
   ```bash
   git clone https://github.com/aubergeRP/aubergeRP.git
   cd aubergeRP
   cp config.example.yaml config.yaml
   ```

2. **Launch with Docker**
   ```bash
   make docker
   ```
   This starts AubergeRP standalone, you will need to plug text/image LLM.
   Use this if you have no GPU or just want to test the app with a remote LLM.

   If you do have a GPU : 
   ```bash
   make docker gpu=rtx3090
   ```
   This starts AubergeRP with a LocalAI instance, which will automatically download and serve the configured text and image models.

   If your model is not listed, use one closer to it in terms of VRAM usage, and edit the `docker/profiles/*.yml` files to set the correct model name for LocalAI.


3. **Enjoy!**
   Open **http://localhost:8123**. The admin password is displayed in your terminal logs.
   * Go to **Admin** -> The LocalAI text connector is pre-configured. Some characters are already provisioned to try out, but you can also import a character and start your story.
   * Image generation works out of the box once the model download completes.



## ⚠️ Deployment Notice

AubergeRP is designed for **personal, self-hosted use on a trusted network** (your home LAN, a VPN, or localhost).

The chat UI has **no user authentication**. Anyone who can reach the server port can read conversations and chat with your characters. The admin panel has its own password, but the chat itself is open.

**Do not expose AubergeRP directly to the internet** without putting it behind a reverse proxy that enforces authentication (e.g. HTTP basic auth via nginx, Authelia, Cloudflare Access, or similar). Full user-facing auth (`app.auth_mode`) is a planned feature — see [TODO.md](TODO.md).

## 🏗 Technology Stack

* **Backend:** Python 3.12+, FastAPI, SQLite.
* **Frontend:** Vanilla HTML/JS + Tailwind CSS (No heavy frameworks).
* **Protocols:** SSE (Server-Sent Events) for real-time streaming.
* **License:** Apache 2.0.


## 📚 Documentation

* 📖 [Installation Guide](docs/installation-guide.md) – Step-by-step setup (Docker, GPU, etc.).
* 🧩 [Connector System](docs/06-connector-system.md) – How to add new AI backends.
* ⚙️ [Configuration](docs/09-configuration-and-setup.md) – `config.yaml` reference.
* 💬 [Chat & Conversations](docs/05-chat-and-conversations.md) – Prompt pipeline, transports, dialogue-only mode.
* ⏰ [Character Schedules & Proactive Messages](docs/07-character-card-schedules.md) – Characters that write first.
* 📊 [Observability & Operations](docs/08-observability.md) – Operations dashboard, diagnostics and Prometheus metrics.
* 🏗 [Architecture](docs/00-architecture-overview.md) – High-level design for contributors.

## About me
I'm Olivier. I have nearly 30 years XP in dev/ops and I'm thrilled of the new AI era that allows me to develop this kind of project without putting too much time into it.
I have a very busy work (I'm CEO), a beautiful family I like to spend time with, and many hobbies.
This project is the 99th priority in my life. I work on it on my (small) free time, and I try to keep it as simple and maintainable as possible for the long term, so I can keep improving it for years to come without it becoming a burden.
If you want to contribute, please maintain this philosophy in mind and try to keep things simple and well documented. 

**AubergeRP** is a labor of love. If you like the project, consider giving it a ⭐ on GitHub!
