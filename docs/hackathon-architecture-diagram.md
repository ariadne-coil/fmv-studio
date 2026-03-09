# FMV Studio Architecture Diagram

This diagram shows the deployed Google Cloud architecture that the Gemini Live Agent Challenge judges will evaluate.

## System Topology

```mermaid
flowchart TB
    user[User Browser]

    subgraph experience[User Experience]
        frontend[Next.js Studio UI<br/>Cloud Run]
        director_ui[Live Director Window<br/>chat + realtime voice]
    end

    subgraph runtime[Agent Runtime]
        backend[FastAPI Orchestrator<br/>Cloud Run]
        live_gateway[Live Director Gateway<br/>Cloud Run WebSocket proxy]
        tasks[Cloud Tasks Queue<br/>async storyboard and filming jobs]
        gcs[GCS Bucket<br/>project state, uploads, frames,<br/>clips, music, final renders]
    end

    subgraph models[Vertex AI Model Layer]
        orch[Gemini Pro<br/>orchestrator and prompt rewriting]
        critic[Gemini Flash<br/>3-critic validation panel]
        image[Gemini Image<br/>storyboard generation]
        veo[Veo 3.1<br/>filming generation]
        audio[Lyria and TTS<br/>music and stage briefs]
        live_model[Gemini Live native audio<br/>realtime director speech]
    end

    user -->|HTTPS| frontend
    user -->|chat + mic input| director_ui
    director_ui -->|same-origin app state| frontend
    frontend -->|REST /api| backend
    frontend <-->|/projects/* media| backend
    director_ui <-->|WebSocket realtime speech| live_gateway
    director_ui -->|tool call executes project edits| backend

    backend <--> |read and write state + assets| gcs
    backend -->|enqueue long-running runs| tasks
    tasks -->|authenticated execute-run callback| backend
    live_gateway <-->|Vertex Live session| live_model

    backend -->|planning, continuity,<br/>prompt rewriting| orch
    backend -->|frame and clip review| critic
    backend -->|generate storyboard frames| image
    backend -->|generate clips from approved frames| veo
    backend -->|generate songs and spoken briefs| audio

    image -->|frames| backend
    veo -->|clips| backend
    audio -->|wav and mp3 assets| backend

    classDef surface fill:#f7f3e8,stroke:#6d5c3d,stroke-width:1.5px,color:#1f1a14;
    classDef service fill:#e4efe7,stroke:#2f6b4f,stroke-width:1.5px,color:#10251c;
    classDef model fill:#e7eef9,stroke:#3c5f99,stroke-width:1.5px,color:#14243f;

    class user,frontend,director_ui surface;
    class backend,live_gateway,tasks,gcs service;
    class orch,critic,image,veo,audio,live_model model;
```

## Agent and Production Stage Flow

```mermaid
flowchart LR
    input[1 Input]
    music_stage[2 Music]
    planning[3 Planning]
    storyboard[4 Storyboarding]
    filming[5 Filming]
    production[6 Production]
    completed[7 Completed]

    orch_music[Gemini Pro<br/>lyrics, style, structure]
    lyria[Lyria or imported song]
    orch_plan[Gemini Pro<br/>shot list, durations, continuity plan]
    frame_gen[Gemini Image<br/>16:9 storyboard frames]
    frame_crit[Gemini Flash<br/>3-critic frame panel]
    clip_gen[Veo 3.1<br/>16:9 1080p clips]
    clip_crit[Gemini Flash<br/>3-critic video panel]
    editor[Production timeline editor<br/>split, reorder, audio edit]
    render[ffmpeg render<br/>final master]
    brief[TTS stage brief<br/>one-time spoken summary]
    director[Live Director<br/>chat + realtime voice]

    input --> music_stage
    music_stage --> orch_music --> lyria --> planning
    planning --> orch_plan --> storyboard
    storyboard --> frame_gen --> frame_crit
    frame_crit -->|approved frames only| filming
    filming --> clip_gen --> clip_crit
    clip_crit -->|approved clips only| production
    production --> editor --> render --> completed

    frame_crit -. failed critique / regenerate .-> storyboard
    clip_crit -. filtered or low-confidence / retry .-> filming
    render -. edit changes or rerender .-> production
    director -. conversational edits, add/delete/reorder shots, stage navigation .-> planning
    director -. frame notes, shot timing, structural changes .-> storyboard
    director -. prompt tweaks, render retries .-> filming
    director -. split/reorder/audio changes, rerender requests .-> production

    planning -. ready summary .-> brief
    storyboard -. ready summary .-> brief
    filming -. ready summary .-> brief
    production -. ready summary .-> brief

    classDef stage fill:#f6eadf,stroke:#9a6b2f,stroke-width:1.5px,color:#2b1b0c;
    classDef agent fill:#e5effa,stroke:#476c9b,stroke-width:1.5px,color:#15253d;
    classDef output fill:#e6f2ea,stroke:#3c7c57,stroke-width:1.5px,color:#11271b;

    class input,music_stage,planning,storyboard,filming,production,completed stage;
    class orch_music,orch_plan,frame_crit,clip_crit,brief agent;
    class lyria,frame_gen,clip_gen,editor,render output;
    class director agent;
```

## Runtime Flow

1. The user interacts with the Next.js frontend on Cloud Run.
2. The frontend sends project updates and pipeline commands to the FastAPI backend.
3. The backend persists project state and generated media into Google Cloud Storage.
4. Long-running storyboard and filming work is queued through Cloud Tasks.
5. The floating Live Director window can send typed commands through the backend or open a realtime voice session through the Live Director gateway.
6. The backend processes jobs and calls Vertex AI models for orchestration, critique, image generation, video generation, and music / voice synthesis.
7. Generated assets are written back to GCS and served to the frontend through the backend's `/projects/...` URLs.

## Notes For Judges

- The same codebase also supports local mode, but the hackathon deployment path is Cloud Run + Vertex AI + GCS + Cloud Tasks.
- The frontend and backend are deployed separately.
- Realtime Live Director voice uses a separate Cloud Run WebSocket gateway that proxies to Vertex AI Live.
- Async runs are durable because project state is persisted outside the Cloud Run instance.
- Model roles are split. Default model selections are as follows:
  - `Gemini Pro` for orchestration and prompt rewriting
  - `Gemini Flash` for critique
  - `Veo` for video
  - `Gemini Image` for storyboards
  - `Lyria / TTS` for music and spoken stage briefs
  - `Gemini Live native audio` for realtime Live Director speech
