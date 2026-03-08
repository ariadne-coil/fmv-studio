# FMV Studio Project Summary

## What FMV Studio Does

FMV Studio is an AI-assisted music video production environment. It is designed to compress the time required to produce a high-quality AI-generated music video from days of manual prompt chaining, file passing, and tool switching down to minutes inside one guided workflow. Instead of forcing the user to juggle separate tools for music generation, planning, image generation, video generation, review, and editing, FMV Studio keeps the whole creative process in one place and moves the user from an initial song idea to a finished edited video through a staged workflow:

1. `Input`: provide concept, screenplay direction, audio, and reference assets.
2. `Music`: generate or import the song and align the creative direction.
3. `Planning`: create a structured shot list and overall visual timeline.
4. `Storyboarding`: generate and review initial keyframes for each shot.
5. `Filming`: generate video clips from approved frames.
6. `Production`: cut, reorder, split, mute audio fragments, and render the final master.

The goal is not only to generate assets, but to give the user a controllable creative pipeline with review points, regeneration loops, and a real production timeline.

## Core Features and Functionality

- AI-generated shot planning from user input and music context
- Storyboard image generation with review and regeneration loops
- Video clip generation from storyboard frames
- Separate orchestrator and critic model roles
- Multi-critic validation for storyboard and filming review
- Background async generation so the UI updates live as frames and clips arrive
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

The frontend provides the studio workflow, stage transitions, live polling during long-running jobs, and the production timeline/editor interface.

### Backend

- `FastAPI`
- `Python 3.12`
- `google-genai`
- `ffmpeg`
- `pydantic`

The backend acts as the central agent runtime. It owns project state, orchestration, generation retries, critique, production rendering, and media persistence.

### Google Cloud and Google AI

- `Cloud Run` for frontend and backend deployment
- `Cloud Tasks` for durable async pipeline jobs
- `Google Cloud Storage` for project state and generated assets
- `Artifact Registry` and `Cloud Build` for container builds and deployment
- `Terraform` for infrastructure-as-code
- `Vertex AI` for model access

### Model Roles

- `Gemini Pro`: orchestration, planning, prompt rewriting, continuity reasoning
- `Gemini Flash`: critique and validation
- `Gemini Image`: storyboard frame generation
- `Veo 3.1`: video generation
- `Lyria / TTS`: music and spoken stage summaries

## Other Data Sources Used

FMV Studio does not depend on a separate external dataset or scraped content source at runtime.

The main runtime data sources are:

- user-provided inputs:
  - screenplay and instruction text
  - uploaded music or test audio
  - uploaded reference images and replacement media
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

### 6. Provider abstraction is worth doing early

As model access paths changed, provider abstraction for music, image, and video generation became important. It made the system easier to adapt to Vertex AI and keeps the codebase ready for future provider additions without rewriting the whole pipeline.

## Outcome

The result is a cloud-deployed, agent-driven music video studio that combines:

- multi-stage orchestration
- live generation workflows
- human review checkpoints
- editable post-generation production tools
- automated Google Cloud deployment

That combination is the main technical contribution of FMV Studio: not just generating assets, but turning multiple Google AI systems into a usable end-to-end creative production workflow.
