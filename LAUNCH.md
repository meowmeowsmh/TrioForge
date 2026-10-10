# 🚀 TrioForge — Launch Kit

Working document. **Not part of the app** — delete it or gitignore it before committing if you prefer.

Repo: https://github.com/meowmeowsmh/TrioForge
Time needed: ~15 minutes for step 1 + 2, then reply to comments for an hour.

> ## ⚠️ READ FIRST — r/LocalLLaMA has a karma gate
>
> r/LocalLLaMA blocks posting until your account has good karma **in that subreddit**.
> If you see *"You can't contribute in this community yet… You have 0 karma"*, then:
>
> **→ Do STEP 2 (Show HN) FIRST.** Hacker News has no karma requirement — you can submit today.
> **→ Do STEP 1 (r/LocalLLaMA) later**, after you've earned karma by commenting there genuinely.
>
> Do **not** try to farm karma with throwaway comments — that gets accounts banned.
> Answer questions you actually know the answer to (see "Building Reddit karma" at the bottom).

---

## ✅ Pre-flight (5 minutes — do not skip)

- [ ] **Check your Reddit account age + karma.** Several subs auto-filter project posts from accounts that are new or low-karma, with *no* explanation. If yours is thin, spend a week commenting in r/LocalLLaMA first.
- [ ] **Open each sub's Rules panel and read the self-promotion rule.** Rules change; this is the #1 cause of legit posts being removed.
- [ ] **Confirm you have a recent screenshot/GIF.** `demo.gif` is in the repo root and is the single best asset you have.
- [ ] **Be logged in** to Reddit and Hacker News.
- [ ] **Block out an hour afterwards.** Early replies are what make the post rank.

---

## 📌 STEP 1 — r/LocalLLaMA (your main shot)

**Submit at:** https://www.reddit.com/r/LocalLLaMA/submit

**Flair:** pick the project/showcase-style flair the sub offers.

**Title (use this one):**

```
I built a local AI workspace where a chat answer becomes a pin you can link — no telemetry, runs on Ollama/llama.cpp
```

**Body (paste as-is):**

```
**What it is**

TrioForge is a self-hosted AI workspace. The thing I cared about: every local AI UI I tried was a chat box that forgets everything. Here, any answer can go to a corkboard as a pin, get rewritten there by the model, and be linked to what you already have. The same app holds your notes ([[wiki-links]], backlinks, graph view) and a coding agent.

**Why I built it**

Two annoyances. One: ask a local model something genuinely useful → it scrolls away → I paste it into Obsidian → I lose the connection. Two: getting llama.cpp actually working (right build for my GPU, layer count, vision projector, context size) was its own weekend project every time.

**What's in it**

- **Providers** — local via Ollama or llama.cpp; API via Groq, DeepSeek, Claude, Gemini, OpenRouter, Hugging Face. Swap freely, keys stay local.
- **llama.cpp managed for you** — detects your GPU (CUDA/Vulkan/Metal/ROCm/SYCL), downloads the correct prebuilt build once, auto-fits GPU layers, pairs the mmproj vision projector, and lets you set context size from the UI. It steps down gracefully on a GPU OOM instead of dying.
- **Corkboard + notes** — pins linked to each other, notes with backlinks and a graph view, Obsidian vault sync.
- **Coding agent** with a live diff panel — plus a full-screen terminal client (forge) with its own agent and file tools.
- **Plugins / skills / MCP** — a plugin is one .py file exposing tools; MCP servers are borrowed as tools; Gmail, Google Calendar and Obsidian connectors (the Obsidian one is fully offline).
- **Also** — RAG document chat, local voice-to-voice (STT→LLM→TTS, no cloud), full-text search across every message, image/video generation via ComfyUI or OpenRouter/Gemini.
- Windows / macOS / Linux / WSL / Docker, installable as a PWA on your phone.

**No telemetry, no account, offline-first.** MIT.

**What it doesn't do** — so you don't waste a clone:
- Not a hosted-API wrapper. You bring your own models and keys.
- Local models are still local models. A 3B stays a 3B; nothing here makes a small model smart.
- Image/video gen needs ComfyUI or an API key — not bundled.
- No native mobile app; the PWA covers the phone case.

**Install** (no .exe, nothing to download):

git clone https://github.com/meowmeowsmh/TrioForge && cd TrioForge && ./forge

Windows: double-click TrioForge.bat. Docker one-liner is in the README.

**Screenshots + full breakdown:** https://github.com/meowmeowsmh/TrioForge

**What I'd genuinely like feedback on:** does the chat→corkboard flow fit how you actually work, or is it a gimmick you'd use twice? I keep going back and forth on it, and I'd rather hear it straight. Also happy to hear what's missing for your setup.
```

**The moment it's posted:** reply to the first real comment within minutes. Check back every ~10 minutes for the first two hours.

---

## 🟠 STEP 2 — Show HN (wait 24–48h after Reddit)

**Submit at:** https://news.ycombinator.com/submit
**URL field:** `https://github.com/meowmeowsmh/TrioForge`
**Title:**

```
Show HN: TrioForge – self-hosted AI workspace where chat answers become linkable pins
```

**Then immediately post this as your OWN first comment** (HN expects the author to show up):

```
Author here. TrioForge is a local AI workspace: Flask server + browser UI, with a terminal client in the same repo.

The design bet is that a chat answer shouldn't be ephemeral. You can send any reply to a corkboard as a pin, have a model rewrite it in place, and link pins to each other — so the conversation turns into something structured. Notes ([[wiki-links]], backlinks, graph) live in the same app.

Practical parts: llama.cpp is auto-managed (GPU detection via --list-devices, correct prebuilt build downloaded once, GPU-layer auto-fit, mmproj pairing, UI-controlled context size, OOM step-down). Providers are pluggable — Ollama/llama.cpp locally, or Groq/DeepSeek/Claude/Gemini/OpenRouter/HF. There's a plugins+MCP layer where a plugin is a single .py file exposing tools, and a coding agent with a live diff panel.

No telemetry, no account, MIT. Data lives in SQLite + JSON under the repo.

Happy to answer anything about the llama.cpp lifecycle management or the tool-calling protocol — local models get a fenced tool-block text fallback when they lack native function calling.
```

> **HN tips:** no marketing adjectives, no emoji, no "excited to share". Answer every technical question factually. HN is allergic to hype and rewards specifics.

---

## 💬 STEP 3 — cross-posts (48h later, reword each)

r/selfhosted · r/opensource · r/SideProject · your Discord/X/Mastodon

```
I built TrioForge — a self-hosted AI workspace. Chat is the input, the board is the output: any answer becomes a pin you can rewrite and link, next to your notes and a coding agent. Local via Ollama/llama.cpp, or any API provider. llama.cpp is auto-installed and tuned for your GPU. No telemetry, no account, MIT.
git clone … && ./forge
https://github.com/meowmeowsmh/TrioForge
```

---

## 🚫 What NOT to do

- ❌ Ask for stars. Ask for feedback. Stars follow engagement; begging gets you removed.
- ❌ Post the identical text to 5 subs in one hour — that reads as spam to both mods and the algorithm.
- ❌ Reply defensively to criticism. "Fair, that's a real limitation" earns far more than arguing.
- ❌ Lead with a bare link. Always give the context paragraph first.
- ❌ Paste an AI-sounding wall of buzzwords. You built something real — say it plainly.

## 🎯 What success looks like

Realistically: **10–50 genuine comments and a modest spike in stars.** That's a *win* — it's the first time humans will have ever seen this repo (your traffic showed zero external referrals). Don't measure it against the 612 bot clones.

---

## 🌱 Building Reddit karma (do this in parallel)

r/LocalLLaMA wants karma **in that subreddit**, so the only honest route is to actually help people there. And you have real expertise — you built a llama.cpp lifecycle manager, a multi-provider client and an agent stack. That is precisely what people ask about daily.

Sort r/LocalLLaMA by **New** and look for these:

- *"llama.cpp keeps OOMing on my 8 GB card"* → GPU-layer offload, KV-cache quantization, context size
- *"Ollama vs llama.cpp — which should I use?"* → you've integrated both; you know the tradeoff
- *"How do I set context size / why does my model forget things?"* → `num_ctx`, KV-cache memory cost
- *"Which quant for X GB of VRAM?"* → Q4_K_M vs Q5_K_M vs Q8_0 tradeoffs
- *"Vision projector / mmproj won't load"* → mmproj pairing by base model name, `--image-min-tokens` pitfalls
- *"Best local model for coding / long context?"*
- Anything about RAG chunking, embeddings, or local STT/TTS

**One or two genuinely helpful replies a day for a week is usually enough.** Real answers in your own words — no templates, no throwaway filler. Filler is how accounts get banned.

**Other channels with no karma gate (post these while you wait):**

| Channel | Notes |
|---|---|
| **Hacker News / Show HN** | ⚠️ **Temporarily restricted for new accounts** (HN is throttling Show HNs after a spam influx). Contribute in comments for a few weeks, then post. |
| **Discord servers** | ⭐ **Best ungated option.** llama.cpp, Ollama and LocalLLaMA servers have project/showcase channels. Fast, responsive, zero gates. |
| **r/SideProject · r/coolgithubprojects · r/opensource** | Permissive; built for project sharing. Good for feedback, weaker for users. |
| **Lemmy** (`!selfhosted@lemmy.world`, `!localllama`) | Federated, generally no gates. |
| **X / Bluesky / Mastodon** | Post the GIF + one-liner. No gates at all. |
| **dev.to / Hashnode** | Write the technical story ("how I made llama.cpp auto-fit GPU layers"). No gate, and it becomes a link you can share everywhere later. |
| **GitHub topics** | 2-minute job, permanent benefit: makes the repo findable in GitHub search. Repo page → ⚙️ next to About → Topics. |
