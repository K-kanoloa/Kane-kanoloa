"""Private session worker. Production launches this inside an offline container."""
import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import threading
from time import monotonic

logging.disable(logging.CRITICAL)
WIRE_LIMIT = 1_048_576
OUTPUT_LIMIT = 65_536
wire_out = sys.stdout
shell = None
inbox: asyncio.Queue


class ToolFailure(Exception):
    def __init__(self, code: str):
        self.code = code


class Capture(io.StringIO):
    def write(self, text: str) -> int:
        if self.tell() + len(text) > OUTPUT_LIMIT:
            raise ToolFailure("OUTPUT_LIMIT_EXCEEDED")
        return super().write(text)


def emit(frame: dict) -> None:
    line = json.dumps(frame, ensure_ascii=True, allow_nan=False)
    if len(line) > WIRE_LIMIT:
        raise ToolFailure("OUTPUT_LIMIT_EXCEEDED")
    wire_out.write(line + "\n")
    wire_out.flush()


async def receive() -> dict:
    frame = await inbox.get()
    if not isinstance(frame, dict):
        raise ToolFailure("INVALID_WORKER_FRAME")
    return frame


async def ipython(code: str) -> dict:
    global shell
    from IPython.core.interactiveshell import InteractiveShell

    if shell is None:
        shell = InteractiveShell()
        shell.history_manager.enabled = False
    with contextlib.redirect_stdout(Capture()) as out, contextlib.redirect_stderr(Capture()):
        result = await shell.run_cell_async(code, store_history=False)
    error = result.error_before_exec or result.error_in_exec
    if error is not None:
        # Tracebacks include source and file contents; expose only the exception type.
        raise ToolFailure("PYTHON_EXECUTION_FAILED:" + type(error).__name__)
    value = None if result.result is None else repr(result.result)
    if value is not None and len(value) > OUTPUT_LIMIT:
        raise ToolFailure("OUTPUT_LIMIT_EXCEEDED")
    return {"stdout": out.getvalue(), "value": value}


async def nooa_execute(payload: dict) -> dict:
    # Neither NOOA nor IPython is imported by the worker's startup path.
    from nooa import Agent, strategy
    from nooa.config import CodeActConfig, PredictConfig
    from nooa.strategies import CodeActStrategy, PredictStrategy
    from nooa.unifiedllm.unifiedllm import LLMResponse, ToolCall, UnifiedLLM
    from pydantic import BaseModel, ConfigDict, Field

    class Answer(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        answer: str = Field(min_length=1, max_length=OUTPUT_LIMIT)
        facts: list[str] = Field(default_factory=list, max_length=100)

    class HostLLM(UnifiedLLM):
        def __init__(self):
            super().__init__(model="kanaloa-session-model")
            self.calls = 0

        @property
        def context_window(self):
            return 32_000

        def count_tokens(self, text):
            return len(str(text)) // 3 + 1

        def call(self, *args, **kwargs):
            raise ToolFailure("SYNC_MODEL_CALL_UNSUPPORTED")

        async def acall(self, messages, tools=None, output_model=None, **kwargs):
            self.calls += 1
            if self.calls > payload["max_model_calls"]:
                raise ToolFailure("MODEL_CALL_LIMIT_EXCEEDED")
            schemas = [{"name": tool.name, "description": tool.description,
                        "parameters": tool.get_parameter_schema()} for tool in (tools or [])]
            emit({"type": "model", "id": self.calls, "payload": {
                "messages": messages, "tools": schemas,
                "output_schema": output_model.model_json_schema() if output_model else None,
            }})
            response = await receive()
            if response.get("type") != "model.result" or response.get("id") != self.calls:
                raise ToolFailure("INVALID_WORKER_FRAME")
            if response.get("error"):
                raise ToolFailure(response["error"])
            data = response["payload"]
            content = data["content"]
            if output_model is not None and not data["tool_calls"]:
                content = output_model.model_validate_json(content)
            return LLMResponse(
                raw_response=None, content=content,
                tool_calls=[ToolCall(**call) for call in data["tool_calls"]],
                finish_reason="tool_calls" if data["tool_calls"] else "stop",
                assistant_message=data["assistant_message"], usage=data.get("usage"),
            )

    llm = HostLLM()
    python_calls = 0

    class Subtask(Agent, llm=llm):
        """Solve one bounded Kanaloa subtask. Methods are your only host capabilities.

        Use self.ipython(code) for persistent Python computation in this session.
        CodeAct's own local variables are ephemeral. Return a typed answer and facts.
        No network, host credentials, or other sessions are accessible.
        """

        async def ipython(self, code: str) -> dict:
            """Execute code in this native session's persistent IPython and read its result."""
            nonlocal python_calls
            if payload.get("python_state") == "ephemeral":
                raise ToolFailure("PYTHON_SESSION_UNAVAILABLE")
            python_calls += 1
            emit({"type": "ipython.call", "id": python_calls, "payload": {"code": code}})
            response = await receive()
            if response.get("type") != "ipython.result" or response.get("id") != python_calls:
                raise ToolFailure("INVALID_WORKER_FRAME")
            if response.get("error"):
                raise ToolFailure(response["error"])
            return response["payload"]

        @strategy(PredictStrategy(config=PredictConfig(max_retries=1, max_tokens=2048)))
        async def predict(self, goal: str) -> Answer:
            """Answer the requested local subtask faithfully; return answer and supporting facts."""
            ...

        @strategy(CodeActStrategy(config=CodeActConfig(
            max_iterations=payload["max_model_calls"], max_retries=1,
            max_tokens=2048, cell_timeout=30,
        )))
        async def solve(self, goal: str) -> Answer:
            """Solve the subtask using Python and self.ipython as appropriate; return answer and facts."""
            ...

    agent = Subtask()
    method = agent.solve if payload.get("strategy") == "codeact" else agent.predict
    result = Answer.model_validate((await method(payload["goal"])).model_dump())
    return {"result": result.model_dump(), "model_calls": llm.calls, "ipython_calls": python_calls}


async def main() -> None:
    global inbox
    inbox = asyncio.Queue(maxsize=4)
    loop = asyncio.get_running_loop()

    def enqueue(frame):
        try:
            inbox.put_nowait(frame)
        except asyncio.QueueFull:
            os._exit(2)

    def read_input():
        # EOF must terminate even a busy cell: no orphan work after the parent dies.
        while True:
            line = sys.stdin.readline(WIRE_LIMIT + 1)
            if not line or len(line) > WIRE_LIMIT:
                os._exit(0)
            try:
                frame = json.loads(line)
                loop.call_soon_threadsafe(enqueue, frame)
            except Exception:
                os._exit(2)

    threading.Thread(target=read_input, daemon=True).start()
    emit({"type": "ready"})
    while True:
        frame = await receive()
        started = monotonic()
        try:
            payload = frame.get("payload", {})
            with contextlib.redirect_stdout(Capture()), contextlib.redirect_stderr(Capture()):
                if frame.get("type") == "ipython":
                    result = await ipython(payload["code"])
                elif frame.get("type") == "nooa":
                    result = await nooa_execute(payload)
                else:
                    raise ToolFailure("INVALID_WORKER_FRAME")
            emit({"type": "result", "payload": result,
                  "duration_ms": round((monotonic() - started) * 1000)})
        except BaseException as error:
            code = error.code if isinstance(error, ToolFailure) else "OUTPUT_VALIDATION_FAILED" if type(error).__name__ in ("ValidationError", "GenerationError") else "PYTHON_INTERNAL_ERROR"
            emit({"type": "error", "code": code, "error_type": type(error).__name__})


if __name__ == "__main__":
    asyncio.run(main())
