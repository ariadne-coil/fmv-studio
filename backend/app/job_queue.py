import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any


LOCAL_PIPELINE_TASKS: dict[str, dict[str, Any]] = {}


def get_job_driver() -> str:
    return (os.getenv("FMV_JOB_DRIVER") or "local").strip().lower()


def get_internal_task_token() -> str | None:
    token = os.getenv("FMV_INTERNAL_TASK_TOKEN", "").strip()
    return token or None


def is_local_pipeline_task_active(project_id: str, run_id: str | None = None) -> bool:
    run = LOCAL_PIPELINE_TASKS.get(project_id)
    if not run:
        return False

    task: asyncio.Task[Any] = run["task"]
    if task.done():
        LOCAL_PIPELINE_TASKS.pop(project_id, None)
        return False

    if run_id and run.get("run_id") != run_id:
        return False
    return True


def register_local_pipeline_task(project_id: str, run_id: str, task: asyncio.Task[Any]) -> None:
    LOCAL_PIPELINE_TASKS[project_id] = {
        "run_id": run_id,
        "task": task,
    }


def clear_local_pipeline_task(project_id: str, run_id: str | None = None) -> None:
    run = LOCAL_PIPELINE_TASKS.get(project_id)
    if not run:
        return
    if run_id and run.get("run_id") != run_id:
        return
    LOCAL_PIPELINE_TASKS.pop(project_id, None)


def cancel_local_pipeline_task(project_id: str) -> None:
    run = LOCAL_PIPELINE_TASKS.pop(project_id, None)
    if not run:
        return

    task: asyncio.Task[Any] = run["task"]
    task.cancel()


async def enqueue_pipeline_job(
    *,
    project_id: str,
    run_id: str,
    payload: dict[str, Any],
    base_url: str | None,
    execute_local: Callable[[], Awaitable[None]],
) -> str:
    driver = get_job_driver()
    if driver == "cloud_tasks":
        await _enqueue_pipeline_job_via_cloud_tasks(
            project_id=project_id,
            payload=payload,
            base_url=base_url,
        )
        return driver

    task = asyncio.create_task(execute_local())
    register_local_pipeline_task(project_id, run_id, task)
    return "local"


async def _enqueue_pipeline_job_via_cloud_tasks(
    *,
    project_id: str,
    payload: dict[str, Any],
    base_url: str | None,
) -> None:
    await asyncio.to_thread(_create_cloud_task, project_id, payload, base_url)


def _create_cloud_task(project_id: str, payload: dict[str, Any], base_url: str | None) -> None:
    try:
        from google.cloud import tasks_v2
    except ImportError as exc:  # pragma: no cover - depends on optional runtime package
        raise RuntimeError(
            "FMV_JOB_DRIVER=cloud_tasks requires the 'google-cloud-tasks' package."
        ) from exc

    gcp_project = os.getenv("FMV_GCP_PROJECT", "").strip()
    location = os.getenv("FMV_CLOUD_TASKS_LOCATION", "").strip()
    queue = os.getenv("FMV_CLOUD_TASKS_QUEUE", "").strip()
    resolved_base_url = (os.getenv("FMV_BASE_URL", "").strip() or (base_url or "").strip())

    missing = [
        env_name
        for env_name, value in (
            ("FMV_GCP_PROJECT", gcp_project),
            ("FMV_CLOUD_TASKS_LOCATION", location),
            ("FMV_CLOUD_TASKS_QUEUE", queue),
            ("FMV_BASE_URL", resolved_base_url),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "FMV_JOB_DRIVER=cloud_tasks is missing required environment variables: "
            + ", ".join(missing)
        )

    url = f"{resolved_base_url.rstrip('/')}/api/internal/projects/{project_id}/execute-run"
    headers = {"Content-Type": "application/json"}
    internal_token = get_internal_task_token()
    if internal_token:
        headers["X-Internal-Task-Token"] = internal_token

    http_request: dict[str, Any] = {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": url,
        "headers": headers,
        "body": json.dumps(payload).encode("utf-8"),
    }

    service_account_email = os.getenv("FMV_CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL", "").strip()
    if service_account_email:
        audience = os.getenv("FMV_CLOUD_TASKS_AUDIENCE", "").strip() or url
        http_request["oidc_token"] = {
            "service_account_email": service_account_email,
            "audience": audience,
        }

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(gcp_project, location, queue)
    client.create_task(
        request={
            "parent": parent,
            "task": {
                "http_request": http_request,
            },
        }
    )
