# interests.proposed.md

Proposed v2 of `interests.md` — your original topics kept, plus the additions argued for in
`PLAN.md` §1.2. Your `interests.md` is untouched; edit this, then rename to `interests.yaml`
in the AI-news repo when you're happy with it.

`weight` scales the rule score. `quota` caps how many items of that topic can reach the digest
(this is what stops AI news from crushing the games and hardware sections). Cut anything you
don't actually want — a shorter list ranks better than a complete one.

```yaml
profile: |
  Software engineer in BC, Canada, building LLM/agent products solo with AI coding agents.
  Works in Python, PostgreSQL/PostGIS, RAG pipelines. Runs local models on an RTX 5070 laptop
  and is building a small k3s home cluster. Plays story-rich open-world RPGs. Wants to know
  what shipped and what he could run or use this week — not who raised money.

topics:
  - name: llm-models-and-updates
    weight: 1.0
    quota: 6
    keywords: [model release, open weights, context window, benchmark, GPT, Claude, Gemini,
               Llama, Qwen, DeepSeek, Mistral, reasoning model, multimodal, pricing, rate limit]

  - name: ai-coding-agents
    weight: 1.2          # highest weight: changes how you work the same week
    quota: 4
    keywords: [Claude Code, Codex, Cursor, Copilot, Aider, OpenCode, Devin, coding agent,
               agentic coding, terminal agent, IDE agent, changelog]

  - name: agent-design-and-frameworks
    weight: 1.0
    quota: 4
    keywords: [agent framework, LangGraph, CrewAI, OpenAI Agents SDK, tool calling, multi-agent,
               orchestration, guardrails, human-in-the-loop, agent memory, planner]

  - name: mcp-ecosystem
    weight: 1.0
    quota: 3
    keywords: [MCP, Model Context Protocol, MCP server, tool server]

  - name: local-inference-and-self-hosting
    weight: 1.0
    quota: 3
    keywords: [Ollama, llama.cpp, vLLM, GGUF, quantization, VRAM, local LLM, self-hosted,
               inference server, LM Studio, k3s, homelab]

  - name: rag-and-retrieval
    weight: 0.8
    quota: 2
    keywords: [RAG, retrieval, embedding, vector database, reranking, chunking, Chroma, Qdrant,
               pgvector, knowledge graph]

  - name: eval-and-observability
    weight: 0.8
    quota: 2
    keywords: [eval, benchmark suite, LLM-as-judge, LangSmith, LangFuse, tracing, observability,
               regression test, hallucination rate]

  - name: speech-and-asr
    weight: 0.7
    quota: 2
    keywords: [ASR, speech recognition, diarization, Whisper, transcription, TTS, voice model,
               real-time audio]

  - name: hardware-consumer
    weight: 0.9
    quota: 3
    keywords: [GPU, CPU, VRAM, RTX, laptop, smartphone, iPhone, Mac, headphones, console,
               benchmark, launch, review]
    lenses:            # rank higher within this topic when these also appear
      - "can I run a model on it"   # VRAM, memory bandwidth, NPU, local inference
      - "prescription"              # smart glasses only matter with integrated Rx lenses

  - name: smart-glasses
    weight: 1.0
    quota: 2
    keywords: [smart glasses, AR glasses, Ray-Ban Meta, Even Realities, Xreal, Halliday,
               prescription lens, heads-up display]
    anti_keywords: [clip-on, over-glasses]

  - name: games-rpg
    weight: 0.9
    quota: 3
    keywords: [CRPG, RPG, open world, CD Projekt, Larian, Baldur's Gate, Witcher, Cyberpunk,
               Obsidian, BioWare, immersive sim, narrative, expansion, patch notes, release date]
    anti_keywords: [mobile, gacha, esports, battle royale, live service, skin bundle]

  - name: game-tech-and-ai
    weight: 0.9
    quota: 2
    keywords: [game AI, NPC, procedural narrative, LLM NPC, game engine, Unreal, Unity, Godot,
               simulation, emergent behaviour]
    note: direct input to project ideas 01 and 02

  - name: events
    weight: 1.0
    quota: 2
    keywords: [keynote, announced at, WWDC, CES, GDC, Gamescom, BlizzCon, The Game Awards,
               Apple Event, Google I/O, NVIDIA GTC, Build, Ignite]
    note: also driven by a hand-maintained date calendar, not just keywords

anti_topics:            # drop outright, whatever the score
  - funding round, raises, valuation, IPO, acquisition rumour
  - "announces partnership with"
  - AI doom, AGI timelines, hype and think-piece opinion essays
  - crypto, blockchain, NFT, token
  - mobile games, gacha, esports results
  - accessories, cases, chargers, "deals", "best X of 2026" listicles
  - titles phrased as a question
  - press releases with no repo, model, changelog or product page to click through to

hard_rules:
  - always_include: [github releases of watched repos, official pricing or rate-limit changes]
  - max_items_per_digest: 20
  - max_summarised: 8
  - min_hn_points: 100
  - max_age_hours: 48
```
