"""LLM backends: Anthropic SDK (API key / ant profile) or the local `claude` CLI."""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(RuntimeError):
    pass


def extract_code(text: str) -> str | None:
    blocks = re.findall(r"```(?:featurescript|fs|javascript|js)?\s*\n(.*?)```", text, re.S)
    blocks = [b for b in blocks if "defineFeature" in b]
    return blocks[-1].strip() + "\n" if blocks else None


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "high"):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model, self.effort = model, effort

    def complete(self, system: str, prompt: str, image: Path | None = None) -> str:
        content: list = []
        if image is not None:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.standard_b64encode(image.read_bytes()).decode()}})
        content.append({"type": "text", "text": prompt})
        # The system prompt (spec + examples) is identical across calls, so cache it.
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=64000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise LLMError(f"model declined: {getattr(msg.stop_details, 'explanation', '')}")
        return "".join(b.text for b in msg.content if b.type == "text")


class ClaudeCLI:
    """Uses the logged-in Claude Code CLI; no API key needed. Images are not passed."""

    name = "claude-cli"

    def __init__(self, model: str | None = None, timeout: int | None = None, web: bool = True):
        if not shutil.which("claude"):
            raise LLMError("`claude` CLI not found")
        self.model, self.web = model, web
        self.timeout = timeout or int(os.environ.get("FSGEN_LLM_TIMEOUT", 1800))

    def complete(self, system: str, prompt: str, image: Path | None = None) -> str:
        tools, allowed = [], []
        if self.web:
            # product dimensions etc. can be looked up like in a chat (uses Claude usage, not Onshape calls)
            tools += ["WebSearch", "WebFetch"]
            allowed += ["WebSearch", "WebFetch"]
        if image is not None:
            # Let the CLI look at the preview with its Read tool (read-only, this one file).
            tools.append("Read")
            allowed.append(f"Read({image.resolve()})")
            prompt = (f"A rendered 4-view preview of the current part is at {image.resolve()} - open it with the "
                      f"Read tool and use it in your check.\n\n{prompt}")
        cmd = ["claude", "-p", "--tools", ",".join(tools), "--no-session-persistence", "--output-format", "text",
               "--system-prompt", system]
        if allowed:
            cmd += ["--allowedTools", *allowed]
        if self.model:
            cmd += ["--model", self.model]
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise LLMError(f"claude CLI did not answer within {self.timeout}s (set FSGEN_LLM_TIMEOUT to allow more)")
        if r.returncode != 0:
            raise LLMError(f"claude CLI failed ({r.returncode}): {r.stderr[-2000:]}")
        return r.stdout


class CommandLLM:
    """Any LLM with a command-line interface: the prompt goes to stdin, the answer comes from stdout.

    Set FSGEN_LLM_COMMAND (or --backend command --llm-command ...), e.g.
        ollama run qwen2.5-coder:32b
        gemini -p
        codex exec -
        llm -m gpt-4o            (Simon Willison's `llm` tool, many providers)
    The system prompt (spec, rules, examples) is sent first in the same input. The preview image is not sent;
    its path is mentioned so tools that can read files may use it.
    """

    name = "command"

    def __init__(self, command: str, timeout: int | None = None):
        import shlex
        if not command:
            raise LLMError("no LLM command: set FSGEN_LLM_COMMAND, e.g. 'ollama run qwen2.5-coder:32b'")
        self.argv = shlex.split(command)
        if not shutil.which(self.argv[0]):
            raise LLMError(f"LLM command not found: {self.argv[0]}")
        self.name = f"command ({self.argv[0]})"
        self.timeout = timeout or int(os.environ.get("FSGEN_LLM_TIMEOUT", 1800))

    def complete(self, system: str, prompt: str, image: Path | None = None) -> str:
        text = f"{system}\n\n---\n\n{prompt}"
        if image is not None:
            text += f"\n\n(A 4-view preview image of the current part is at {image.resolve()}.)"
        try:
            r = subprocess.run(self.argv, input=text, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise LLMError(f"{self.argv[0]} did not answer within {self.timeout}s (FSGEN_LLM_TIMEOUT)")
        if r.returncode != 0:
            raise LLMError(f"{self.argv[0]} failed ({r.returncode}): {r.stderr[-2000:]}")
        return r.stdout


def make_llm(backend: str = "auto", model: str | None = None, web: bool = True, command: str | None = None):
    command = command or os.environ.get("FSGEN_LLM_COMMAND")
    if backend == "command" or (backend == "auto" and command):
        return CommandLLM(command or "")
    if backend == "anthropic" or (backend == "auto" and (os.environ.get("ANTHROPIC_API_KEY")
                                                          or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                                                          or Path("~/.config/anthropic").expanduser().exists())):
        return AnthropicLLM(model or DEFAULT_MODEL)
    return ClaudeCLI(model, web=web)
