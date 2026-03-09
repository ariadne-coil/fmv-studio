# FMV Studio Project Summary

## What FMV Studio Does

FMV Studio is an AI-assisted music video production environment. It is designed to compress the time required to produce a high-quality AI-generated music video from days of manual prompt chaining, file passing, and tool switching down to minutes inside one guided workflow. Instead of forcing the user to juggle separate tools for music generation, planning, image generation, video generation, review, and editing, FMV Studio keeps the whole creative process in one place and moves the user from an initial song idea to a finished edited video through a staged workflow. On top of that structured pipeline, it adds a `Live Director` agent the user can talk to in chat or realtime voice to reshape the project while it is underway:

1. `Input`: provide concept, screenplay direction, audio, and reference assets.
2. `Music`: generate or import the song and align the creative direction.
3. `Planning`: create a structured shot list and overall visual timeline.
4. `Storyboarding`: generate and review initial keyframes for each shot.
5. `Filming`: generate video clips from approved frames.
6. `Production`: cut, reorder, split, mute audio fragments, and render the final master.

The goal is not only to generate assets, but to give the user a controllable creative pipeline with review points, regeneration loops, and a real production timeline.

## Core Features and Functionality

- AI-generated shot planning from user input and music context
- Realtime `Live Director` control with typed chat and direct voice interaction
- Storyboard image generation with review and regeneration loops
- Video clip generation from storyboard frames
- Separate orchestrator and critic model roles
- Multi-critic validation for storyboard and filming review
- Background async generation so the UI updates live as frames and clips arrive
- Conversational editing of shot text, shot order, shot timing, stage navigation, and production audio decisions
- Editable production timeline with split, reorder, and separate audio muting per fragment
- Export of final renders and project resources
- Saved projects with rename, reopen, and delete flows
- Optional spoken stage summaries for stage-ready review
- Cloud deployment path with persistent project state and async job dispatch

## Technologies Used

### Frontend

- `Next.js 16`
- `React 19`
- `TypeScript`
- `Tailwind CSS 4`

The frontend provides the studio workflow, stage transitions, live polling during long-running jobs, the floating Live Director window, and the production timeline/editor interface.

### Backend

- `FastAPI`
- `Python 3.12`
- `google-genai`
- `ffmpeg`
- `pydantic`

The backend acts as the central agent runtime. It owns project state, orchestration, generation retries, critique, Live Director command handling, production rendering, and media persistence.

### Google Cloud and Google AI

- `Cloud Run` for frontend and backend deployment
- `Cloud Run` for the public Live Director realtime gateway
- `Cloud Tasks` for durable async pipeline jobs
- `Google Cloud Storage` for project state and generated assets
- `Artifact Registry` and `Cloud Build` for container builds and deployment
- `Terraform` for infrastructure-as-code
- `Vertex AI` for model access

### Model Roles

- `Gemini 3.1 Pro`: orchestration, planning, prompt rewriting, continuity reasoning
- `Gemini 3 Flash`: critique and validation
- `Gemini Image 2`: storyboard frame generation
- `Veo 3.1`: video generation
- `Lyria 2`: music
- `Gemini TTS`: spoken stage summaries
- `Gemini Live native audio`: realtime Live Director speech input/output

## Other Data Sources Used

FMV Studio does not depend on a separate external dataset or scraped content source at runtime.

The main runtime data sources are:

- user-provided inputs:
  - screenplay and instruction text
  - uploaded music or test audio
  - uploaded reference images and replacement media
  - typed and spoken Live Director instructions
- generated assets:
  - storyboard frames
  - video clips
  - music outputs
  - stage summary audio
- persisted project state:
  - project JSON stored in local mode or Google Cloud Storage

For testing and smoke checks, the repo also includes a small fixture audio file in [`tests/fixtures/test_audio.mp3`](../tests/fixtures/test_audio.mp3).

## Findings and Learnings

### 1. Long-running generative workflows need visible progress

One of the earliest UX failures was making the user sit on a `Processing` button with no movement for storyboarding or filming. Moving those stages to async background runs with live UI refresh made the product feel substantially more usable and trustworthy.

### 2. Prompt shape matters as much as model choice

For Veo in particular, prompt structure had a major effect on reliability. Dense storyboard descriptions produced more filtered or failed generations than shorter, motion-oriented prompts. The system now treats prompt rewriting as part of orchestration rather than assuming the original storyboard text is already suitable for image-to-video generation.

### 3. Critique systems should be skeptical of themselves

A single critic often hallucinated issues. Moving to a three-critic consensus pattern reduced false positives and made review behavior more stable. This was an important lesson in using models as evaluators: consistency and disagreement handling matter as much as raw intelligence.

### 4. Cloud deployment changes architecture, not just hosting

The local prototype could rely on in-memory job tracking and local files. The cloud version could not. Moving to Cloud Run + Cloud Tasks + GCS required durable state, queue-based async execution, and storage abstraction. This was one of the most important architectural shifts in the project.

### 5. Users need control after generation, not just before it

Generation alone was not enough. The production stage became much stronger once the user could split clips, reorder them, mute audio fragments independently, export assets, and revise the cut without rerunning the entire pipeline.

### 6. Live interaction changes how agent tooling should feel

Once the `Live Director` existed, the product stopped feeling like a sequence of forms and started feeling like a collaborator. That only worked once the director could do real work: rewrite shots, add/delete/reorder them, move the project between stages, and speak back in realtime instead of only returning text or pre-rendered TTS.

### 7. Provider abstraction is worth doing early

As model access paths changed, provider abstraction for music, image, and video generation became important. It made the system easier to adapt to Vertex AI and keeps the codebase ready for future provider additions without rewriting the whole pipeline.

## Outcome

The result is a cloud-deployed, agent-driven music video studio that combines:

- multi-stage orchestration
- realtime multimodal directing
- live generation workflows
- human review checkpoints
- editable post-generation production tools
- automated Google Cloud deployment

That combination is the main technical contribution of FMV Studio: not just generating assets, but turning multiple Google AI systems into a usable end-to-end creative production workflow with both structured stages and live conversational control.

## How This Differs From Flow

Flow is an AI filmmaking tool built around Google models such as Veo, Imagen, and Gemini for scene creation and cinematic control. This sounds similar, but FMV Studio differs in several important ways:

- FMV Studio is explicitly music-video-centric.
  - The workflow starts from song, lyrics, pacing, and beat-aligned visual planning rather than from open-ended scene generation.
- FMV Studio is stage-driven and agentic.
  - Instead of a single creation surface, it moves the user through `Music`, `Planning`, `Storyboarding`, `Filming`, and `Production`, with orchestration and critique happening between stages.
- FMV Studio also includes a persistent directing agent.
  - The user can talk to `Live Director` in chat or realtime voice and ask for changes in natural language without dropping out of the main workflow.
- FMV Studio treats review and regeneration as first-class workflow steps.
  - Storyboards and clips are critiqued, retried, and approved before they become inputs to later stages.
- FMV Studio includes an integrated post-generation production pass.
  - The user can split, reorder, mute audio fragments, and render a final master inside the same system.
- FMV Studio is built as a cloud-deployed agent architecture, not just a creator UI.
  - Project state, async jobs, generated media, and deployment automation are part of the product design itself.

What makes FMV Studio different from Flow is that FMV Studio wraps Google model capabilities in a structured music video production pipeline with durable state, critique loops, and editable production tooling.

The biggest UX difference is that FMV Studio keeps the user oriented around the full creative arc at every stage. The user can always understand where the project stands globally, what is ready, what needs attention, and how the current stage affects the final video. Shot-level detail is available when needed, but the system does not force the user to micromanage every individual frame or clip just to keep moving. That balance between high-level control and optional low-level intervention is central to the product design.
