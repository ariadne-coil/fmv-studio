from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from enum import Enum

class AgentStage(str, Enum):
    INPUT = "input"
    LYRIA_PROMPTING = "lyria_prompting"
    PLANNING = "planning"
    STORYBOARDING = "storyboarding"
    FILMING = "filming"
    PRODUCTION = "production"
    HALTED_FOR_REVIEW = "halted_for_review"
    COMPLETED = "completed"


class PipelineRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"

class MediaAsset(BaseModel):
    id: str
    url: str # Path to local or uploaded file
    type: str # "image", "audio", "video"
    name: str

class VideoClip(BaseModel):
    id: str
    timeline_start: float # in seconds
    duration: float # in seconds
    storyboard_text: str # Gemini's description
    
    # Storyboarding Phase (NanoBanana 2)
    image_prompt: Optional[str] = None
    image_url: Optional[str] = None
    image_critiques: List[str] = Field(default_factory=list)
    image_approved: Optional[bool] = None
    image_score: Optional[int] = None
    image_reference_ready: bool = False

    # Filming Phase (Veo 3.1)
    video_prompt: Optional[str] = None
    video_url: Optional[str] = None
    video_quality: str = "fast" # 'fast' | 'quality'
    video_critiques: List[str] = Field(default_factory=list)
    video_score: Optional[int] = None
    video_approved: Optional[bool] = None


class ProductionTimelineFragment(BaseModel):
    id: str
    source_clip_id: str
    timeline_start: float
    source_start: float = 0.0
    duration: float
    audio_enabled: bool = True


class PipelineRunState(BaseModel):
    run_id: str
    stage: AgentStage
    status: PipelineRunStatus
    driver: str = "local"
    started_at: str
    updated_at: str


class StageSummary(BaseModel):
    text: str
    audio_url: Optional[str] = None
    generated_at: str

class ProjectState(BaseModel):
    project_id: str
    name: str
    current_stage: AgentStage = AgentStage.INPUT
    
    # User Inputs
    screenplay: str = ""
    instructions: str = ""
    additional_lore: str = ""
    music_url: Optional[str] = None # None implies need to generate with Lyria
    image_provider: Optional[str] = None
    video_provider: Optional[str] = None
    music_provider: Optional[str] = None
    music_workflow: str = "lyria3"
    lyrics_prompt: str = ""
    style_prompt: str = ""
    music_min_duration_seconds: Optional[float] = None
    music_max_duration_seconds: Optional[float] = None
    generated_music_provider: Optional[str] = None
    generated_music_lyrics_prompt: Optional[str] = None
    generated_music_style_prompt: Optional[str] = None
    generated_music_min_duration_seconds: Optional[float] = None
    generated_music_max_duration_seconds: Optional[float] = None
    veo_quality: str = "fast"
    assets: List[MediaAsset] = Field(default_factory=list)
    
    # Agent State
    timeline: List[VideoClip] = Field(default_factory=list)
    production_timeline: List[ProductionTimelineFragment] = Field(default_factory=list)
    final_video_url: Optional[str] = None
    last_error: Optional[str] = None
    active_run: Optional[PipelineRunState] = None
    stage_summaries: Dict[str, StageSummary] = Field(default_factory=dict)
