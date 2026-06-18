import os
import json
import glob
from datetime import datetime
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from app.config import DATA_DIR


CONTENT_TYPES = [
    "game_stream", "podcast", "talking_head",
    "tutorial", "presentation", "vlog",
    "cinematic", "vertical", "unknown"
]

LAYOUT_TYPES = [
    "center_crop", "split_cam", "inset", "face_track"
]

LAYOUT_MODES = ["auto", "game_stream", "podcast", "talking_head", "tutorial", "presentation", "center_crop"]

ASPECT_RATIOS = ["9:16", "1:1", "4:5", "16:9"]
AR_DIMS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (864, 1080), "16:9": (1920, 1080)}

# Target clip-length presets offered in the create form. Each maps to a
# (min_seconds, max_seconds) window the moment detector aims for.
CLIP_DURATION_PRESETS = {
    "auto": (15, 60),
    "10-15": (10, 15),
    "15-30": (15, 30),
    "30-45": (30, 45),
    "45-60": (45, 60),
    "60-90": (60, 90),
}


def resolve_clip_duration(preset: Optional[str]) -> tuple:
    """Return the (min, max) seconds for a clip-duration preset, defaulting to 'auto'."""
    return CLIP_DURATION_PRESETS.get(preset or "auto", CLIP_DURATION_PRESETS["auto"])

# Ordered pipeline stages. Each can be re-run independently from the job detail
# page; retrying a stage re-runs it and every stage that depends on it.
PIPELINE_STEPS = [
    {"id": "download", "label": "Download", "desc": "Fetch the source video"},
    {"id": "transcribe", "label": "Transcribe", "desc": "Speech-to-text + cleanup"},
    {"id": "moments", "label": "Find moments", "desc": "LLM scores clip-worthy moments"},
    {"id": "classify", "label": "Classify scene", "desc": "Detect content type / layout"},
    {"id": "diarize", "label": "Diarize", "desc": "Identify who is speaking"},
    {"id": "render", "label": "Render clips", "desc": "Reframe + burn captions"},
]

# Maps an in-progress job.status to the pipeline step it represents.
_STATUS_STEP = {
    "downloading": "download",
    "transcribing": "transcribe",
    "finding_moments": "moments",
    "classifying": "classify",
    "diarizing": "diarize",
    "rendering": "render",
}


class ClipCropOverrides(BaseModel):
    pan_x: float = 0
    pan_y: float = 0
    zoom: float = 1.0

class ClipSubtitleEdit(BaseModel):
    index: int
    text: str
    start_offset: float = 0

class SubtitleStyleOverrides(BaseModel):
    font_size_pct: float = 100
    position: str = "bottom"  # top, center, bottom
    color: Optional[str] = None

class CompositionClip(BaseModel):
    clip_id: str
    order: int
    trim_start: Optional[float] = None
    trim_end: Optional[float] = None

class Composition(BaseModel):
    id: str
    job_id: str
    title: str = "Untitled compilation"
    clips: List[CompositionClip] = []
    transition: str = "cut"
    transition_duration: float = 0.5
    status: str = "draft"
    file_path: Optional[str] = None
    download_url: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

class Clip(BaseModel):
    id: str
    title: str
    hook: str
    reason: str
    score: int
    start: float
    end: float
    duration: float
    hook_type: Optional[str] = None
    standalone_check: Optional[str] = None
    title_alternatives: List[str] = []
    emphasis: List[Dict] = []
    file_path: Optional[str] = None
    download_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    facecam_detected: bool = False
    layout_used: str = "center_crop"
    speaker_label: Optional[str] = None
    speaker_segments: Optional[List[Dict]] = None
    trim_start: Optional[float] = None
    trim_end: Optional[float] = None
    crop_overrides: Optional[ClipCropOverrides] = None
    subtitle_edits: List[ClipSubtitleEdit] = []
    subtitle_style: Optional[SubtitleStyleOverrides] = None
    favorite: bool = False

class Job(BaseModel):
    id: str
    source_url: Optional[str] = None
    file_name: Optional[str] = None
    status: str = "queued"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    max_clips: int = 5
    lang: Optional[str] = None
    content_type: Optional[str] = None
    layout_mode: str = "auto"
    aspect_ratio: str = "9:16"
    audio_ducking: bool = False
    clip_start: Optional[float] = None
    clip_end: Optional[float] = None
    thumbnail_url: Optional[str] = None
    clips: List[Clip] = []
    error: Optional[str] = None
    speaker_count: Optional[int] = None
    caption_style: str = "bold_pop"
    clip_duration: str = "auto"
    dense_cut: bool = False
    download_resolution: str = "1080p"
    silence_ranges: List[List[float]] = []
    download_pct: Optional[float] = None
    download_downloaded_mb: Optional[float] = None
    download_total_mb: Optional[float] = None
    failed_step: Optional[str] = None

    def pipeline_steps(self) -> List[Dict]:
        """Return each pipeline step annotated with its current state
        (done / running / failed / pending), derived from on-disk artifacts and
        the current job status. Used by the job detail page to drive per-step
        retry buttons."""
        job_dir = os.path.join(DATA_DIR, "jobs", self.id)

        def _has(name: str) -> bool:
            return os.path.exists(os.path.join(job_dir, name))

        rendered = bool(self.clips) and all(c.file_path for c in self.clips)
        done_map = {
            "download": bool(glob.glob(os.path.join(job_dir, "source.*"))),
            "transcribe": _has("segments_raw.json"),
            "moments": len(self.clips) > 0,
            "classify": bool(self.content_type),
            "diarize": _has("diarization.json"),
            "render": rendered,
        }

        clean_status = self.status.split(" ")[0]
        running_step = _STATUS_STEP.get(clean_status)

        # moments/classify/diarize run in parallel; any of their statuses
        # means all three are concurrently active.
        _PARALLEL_STATUSES = {"finding_moments", "classifying", "diarizing"}
        _PARALLEL_STEP_IDS = {"moments", "classify", "diarize"}
        in_parallel_phase = clean_status in _PARALLEL_STATUSES

        # If the job failed without recording which step broke, blame the first
        # step that never produced its artifact.
        first_pending = next((s["id"] for s in PIPELINE_STEPS if not done_map.get(s["id"])), None)
        fail_step = self.failed_step or (first_pending if self.status == "failed" else None)

        steps = []
        for s in PIPELINE_STEPS:
            sid = s["id"]
            if done_map.get(sid):
                state = "done"
            elif self.status == "failed" and sid == fail_step:
                state = "failed"
            elif in_parallel_phase and sid in _PARALLEL_STEP_IDS:
                # All three siblings run concurrently — show all as running
                state = "running"
            elif running_step == sid:
                state = "running"
            else:
                state = "pending"
            steps.append({**s, "state": state})
        return steps

    def get_dir(self) -> str:
        job_dir = os.path.join(DATA_DIR, "jobs", self.id)
        os.makedirs(job_dir, exist_ok=True)
        return job_dir

    def save(self):
        self.updated_at = datetime.utcnow().isoformat()
        job_dir = self.get_dir()
        file_path = os.path.join(job_dir, "job.json")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, job_id: str) -> Optional["Job"]:
        file_path = os.path.join(DATA_DIR, "jobs", job_id, "job.json")
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return cls(**data)
        except Exception:
            return None

    @classmethod
    def get_all(cls) -> List["Job"]:
        pattern = os.path.join(DATA_DIR, "jobs", "*", "job.json")
        job_files = glob.glob(pattern)
        jobs = []
        for file_path in job_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    jobs.append(cls(**data))
            except Exception:
                continue
        jobs.sort(key=lambda x: x.created_at, reverse=True)
        return jobs

    def get_compositions(self) -> List["Composition"]:
        comp_dir = os.path.join(DATA_DIR, "jobs", self.id, "compositions")
        if not os.path.exists(comp_dir):
            return []
        comps = []
        for fname in os.listdir(comp_dir):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(comp_dir, fname)) as f:
                        comps.append(Composition(**json.load(f)))
                except Exception:
                    continue
        return sorted(comps, key=lambda c: c.created_at, reverse=True)

    def save_composition(self, comp: Composition):
        comp_dir = os.path.join(DATA_DIR, "jobs", self.id, "compositions")
        os.makedirs(comp_dir, exist_ok=True)
        path = os.path.join(comp_dir, f"{comp.id}.json")
        with open(path, "w") as f:
            f.write(comp.model_dump_json(indent=2))

    def load_composition(self, comp_id: str) -> Optional["Composition"]:
        path = os.path.join(DATA_DIR, "jobs", self.id, "compositions", f"{comp_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return Composition(**json.load(f))
        except Exception:
            return None
