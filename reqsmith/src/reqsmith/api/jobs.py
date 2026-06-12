from fastapi import APIRouter, HTTPException

from reqsmith.persistence.db import session_scope
from reqsmith.persistence.repo import JobRepo, RunRepo

router = APIRouter()


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
    async with session_scope() as session:
        job = await JobRepo(session).get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        run = await RunRepo(session).get(job.run_id)
        return {
            "job_id": job.id,
            "run_id": job.run_id,
            "stage": job.stage,
            "status": job.status.value,
            "attempt": job.attempt,
            "error": job.error,
            "run_state": run.state.value if run else None,
        }


@router.get("/runs/{run_id}")
async def run_status(run_id: str):
    async with session_scope() as session:
        run = await RunRepo(session).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        jobs = await JobRepo(session).for_run(run_id)
        return {
            "run_id": run.id,
            "jira_issue_key": run.jira_issue_key,
            "state": run.state.value,
            "risk_tier": run.risk_tier.value if run.risk_tier else None,
            "policy_version": run.policy_version,
            "prompt_pack_version": run.prompt_pack_version,
            "jobs": [
                {"job_id": j.id, "stage": j.stage, "status": j.status.value, "attempt": j.attempt}
                for j in jobs
            ],
        }
