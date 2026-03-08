#!/usr/bin/env bash
set -euo pipefail

APP_URL="${APP_URL:-${FRONTEND_URL:-${BACKEND_URL:-${1:-}}}}"
if [[ -z "${APP_URL}" ]]; then
  echo "Usage: APP_URL=https://... $0"
  exit 1
fi

ORCHESTRATOR_MODEL="${ORCHESTRATOR_MODEL:-gemini-2.5-flash}"
FIXTURE_PATH="${FIXTURE_PATH:-tests/fixtures/test_audio.mp3}"
PROJECT_ID="${PROJECT_ID:-smoke_$(date -u +%Y%m%d_%H%M%S)}"
PROJECT_NAME="${PROJECT_NAME:-Cloud Smoke Test}"

if [[ ! -f "${FIXTURE_PATH}" ]]; then
  echo "Fixture audio not found: ${FIXTURE_PATH}"
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

export PROJECT_ID PROJECT_NAME

CREATE_PAYLOAD="${TMP_DIR}/create.json"
UPDATE_PAYLOAD="${TMP_DIR}/update.json"
RUN_RESPONSE="${TMP_DIR}/run-response.json"
LIST_RESPONSE="${TMP_DIR}/list-response.json"
HEADERS_FILE="${TMP_DIR}/headers.txt"

export CREATE_PAYLOAD UPDATE_PAYLOAD RUN_RESPONSE LIST_RESPONSE

python3 > "${CREATE_PAYLOAD}" <<'PY'
import json
import os

project_id = os.environ["PROJECT_ID"]
project_name = os.environ["PROJECT_NAME"]

payload = {
    "project_id": project_id,
    "name": project_name,
    "current_stage": "input",
    "screenplay": "Smoke test only. A performer stands beneath a single spotlight and the scene fades out.",
    "instructions": "Keep the plan compact and coherent. This is a deployment smoke test.",
    "additional_lore": "",
    "music_url": None,
    "image_provider": None,
    "video_provider": None,
    "music_provider": None,
    "music_workflow": "uploaded_track",
    "lyrics_prompt": "",
    "style_prompt": "",
    "music_min_duration_seconds": None,
    "music_max_duration_seconds": None,
    "generated_music_provider": None,
    "generated_music_lyrics_prompt": None,
    "generated_music_style_prompt": None,
    "generated_music_min_duration_seconds": None,
    "generated_music_max_duration_seconds": None,
    "veo_quality": "fast",
    "assets": [],
    "timeline": [],
    "production_timeline": [],
    "final_video_url": None,
    "last_error": None,
    "active_run": None,
    "stage_summaries": {},
}
print(json.dumps(payload))
PY

echo "Checking backend availability..."
curl -fsS "${APP_URL%/}/api/health" > /dev/null

echo "Creating project ${PROJECT_ID}..."
curl -fsS \
  -X POST \
  "${APP_URL%/}/api/projects" \
  -H "Content-Type: application/json" \
  --data @"${CREATE_PAYLOAD}" > /dev/null

echo "Uploading fixture audio..."
UPLOAD_RESPONSE="$(
  curl -fsS \
    -X POST \
    "${APP_URL%/}/api/projects/${PROJECT_ID}/upload" \
    -F "file=@${FIXTURE_PATH};type=audio/mpeg"
)"
UPLOAD_URL="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])' <<< "${UPLOAD_RESPONSE}")"
export UPLOAD_URL

echo "Verifying uploaded media route..."
curl -fsS "${APP_URL%/}${UPLOAD_URL}" -o /dev/null -D "${HEADERS_FILE}"

python3 > "${UPDATE_PAYLOAD}" <<'PY'
import json
import os

create_payload_path = os.environ["CREATE_PAYLOAD"]
upload_url = os.environ["UPLOAD_URL"]

with open(create_payload_path, "r", encoding="utf-8") as fh:
    payload = json.load(fh)

payload["music_url"] = upload_url
payload["assets"] = [
    {
        "id": "uploaded_audio",
        "url": upload_url,
        "type": "audio",
        "name": os.path.basename(upload_url),
    }
]

print(json.dumps(payload))
PY

echo "Saving uploaded-track state..."
curl -fsS \
  -X PUT \
  "${APP_URL%/}/api/projects/${PROJECT_ID}" \
  -H "Content-Type: application/json" \
  --data @"${UPDATE_PAYLOAD}" > /dev/null

echo "Running a planning-only smoke pass..."
curl -fsS \
  -X POST \
  "${APP_URL%/}/api/projects/${PROJECT_ID}/run" \
  -H "X-Orchestrator-Model: ${ORCHESTRATOR_MODEL}" \
  -H "X-Stage-Voice-Briefs-Enabled: false" \
  > "${RUN_RESPONSE}"

curl -fsS "${APP_URL%/}/api/projects" > "${LIST_RESPONSE}"

python3 - <<'PY'
import json
import os
import sys

project_id = os.environ["PROJECT_ID"]

with open(os.environ["RUN_RESPONSE"], "r", encoding="utf-8") as fh:
    run_state = json.load(fh)
with open(os.environ["LIST_RESPONSE"], "r", encoding="utf-8") as fh:
    project_list = json.load(fh)

errors = []
if run_state.get("project_id") != project_id:
    errors.append(f"Unexpected project_id in run response: {run_state.get('project_id')}")
if run_state.get("current_stage") != "planning":
    errors.append(f"Expected current_stage=planning, got {run_state.get('current_stage')}")
if run_state.get("last_error") not in (None, ""):
    errors.append(f"Unexpected last_error: {run_state.get('last_error')}")
timeline = run_state.get("timeline") or []
if not timeline:
    errors.append("Planning produced an empty timeline")
if run_state.get("music_url") in (None, ""):
    errors.append("music_url was not retained after planning")
planning_summary = (run_state.get("stage_summaries") or {}).get("planning") or {}
if not planning_summary.get("text"):
    errors.append("Missing planning stage summary text")

listed = None
for item in project_list:
    if item.get("project_id") == project_id:
        listed = item
        break
if listed is None:
    errors.append("Project did not appear in list_projects")
elif listed.get("current_stage") != "planning":
    errors.append(f"List endpoint stage mismatch: {listed.get('current_stage')}")

if errors:
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    sys.exit(1)

print("PASS")
print(f"project_id={project_id}")
print(f"stage={run_state['current_stage']}")
print(f"timeline_clips={len(timeline)}")
print(f"music_url={run_state['music_url']}")
print(f"summary={planning_summary['text'][:160]}")
PY
