import asyncio

import pytest
from pydantic import ValidationError

from app.api.routes import trial_ui
from app.api.routes.trial_ui import PAGE_FILE, TrialPreviewRequest, _event, trial_page


async def test_trial_page_is_available():
    page = PAGE_FILE.read_text(encoding="utf-8")
    response = await trial_page()

    assert "真人 LLM 试验台" in page
    assert "/api/v1/trial/preview" in page
    assert response.path == PAGE_FILE


def test_trial_request_requires_a_key_and_emits_sse():
    with pytest.raises(ValidationError):
        TrialPreviewRequest(base_url="https://api.example.com/v1", api_key="", model="GLM-5.3")

    assert _event("status", {"message": "ready"}) == (
        'event: status\ndata: {"message": "ready"}\n\n'
    )


@pytest.mark.asyncio
async def test_sse_disconnect_terminates_and_awaits_child(monkeypatch):
    class HangingStdout:
        async def readline(self):
            await asyncio.Event().wait()

    class Child:
        def __init__(self):
            self.stdout = HangingStdout()
            self.returncode = None
            self.terminated = False
            self.waited = False

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        async def wait(self):
            self.waited = True
            return self.returncode

    child = Child()

    async def spawn(*args, **kwargs):
        return child

    monkeypatch.setattr(trial_ui.asyncio, "create_subprocess_exec", spawn)
    stream = trial_ui._command_output(["fake"], {})
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.terminated and child.waited
