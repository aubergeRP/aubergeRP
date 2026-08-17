from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx

from ..models.connector import ComfyUIConfig
from ..utils.retry import retry_async
from .base import ImageConnector

_PROMPT_PLACEHOLDER = "__PROMPT__"
_NEGATIVE_PLACEHOLDER = "__NEGATIVE__"

# Built-in workflow templates shipped with the package
_BUILTIN_WORKFLOWS_DIR = Path(__file__).parent.parent / "comfyui_workflows"


class ComfyUIConnector(ImageConnector):
    backend_id = "comfyui"

    def __init__(self, config: ComfyUIConfig, workflows_dir: Path) -> None:
        self.config = config
        self._workflows_dir = Path(workflows_dir)

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _http_url(self, path: str) -> str:
        base = self.config.base_url.rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    def _ws_url(self, client_id: str) -> str:
        base = self.config.base_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[8:]
        elif base.startswith("http://"):
            base = "ws://" + base[7:]
        return f"{base}/ws?clientId={client_id}"

    # ------------------------------------------------------------------
    # Workflow helpers
    # ------------------------------------------------------------------

    def _load_workflow(self) -> dict[str, Any]:
        """Load a named workflow template; user dir takes priority over built-in."""
        name = self.config.workflow or "default"
        user_path = self._workflows_dir / f"{name}.json"
        builtin_path = _BUILTIN_WORKFLOWS_DIR / f"{name}.json"

        if user_path.exists():
            raw = json.loads(user_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return cast(dict[str, Any], raw)
            raise ValueError(f"Workflow '{name}' in {user_path} must be a JSON object")
        if builtin_path.exists():
            raw = json.loads(builtin_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return cast(dict[str, Any], raw)
            raise ValueError(f"Workflow '{name}' in {builtin_path} must be a JSON object")
        raise FileNotFoundError(
            f"Workflow '{name}' not found in {self._workflows_dir} or built-in templates"
        )

    @staticmethod
    def _inject_prompt(
        workflow: dict[str, Any], prompt: str, negative_prompt: str
    ) -> dict[str, Any]:
        """Replace placeholder strings in the workflow JSON."""
        raw = json.dumps(workflow)
        safe_prompt = prompt.replace("\\", "\\\\").replace('"', '\\"')
        safe_neg = negative_prompt.replace("\\", "\\\\").replace('"', '\\"')
        raw = raw.replace(_PROMPT_PLACEHOLDER, safe_prompt)
        raw = raw.replace(_NEGATIVE_PLACEHOLDER, safe_neg)
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Injected workflow must remain a JSON object")
        return cast(dict[str, Any], parsed)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def test_connection(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(self._http_url("/system_stats"))
                response.raise_for_status()
                return {"connected": True, "details": response.json()}
        except Exception as exc:
            return {"connected": False, "details": {"error": str(exc)}}

    async def generate_image(
        self,
        prompt: str,
        negative_prompt: str = "",
        model: str | None = None,
        size: str | None = None,
        reference_image: bytes | None = None,
    ) -> bytes:
        """Generate an image and return raw bytes (no progress reporting)."""
        async for event in self.generate_image_with_progress(prompt, negative_prompt, model, size):
            if event["type"] == "complete":
                payload = event.get("bytes")
                if isinstance(payload, (bytes, bytearray)):
                    return bytes(payload)
                raise RuntimeError("ComfyUI completion event missing bytes payload")
        raise RuntimeError("ComfyUI image generation did not produce a result")

    async def generate_image_with_progress(
        self,
        prompt: str,
        negative_prompt: str = "",
        model: str | None = None,
        size: str | None = None,
        reference_image: bytes | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield progress events then a final complete event with raw image bytes.

        ``reference_image`` is ignored: img2img is expressed by the workflow
        template itself, not by the request body.
        """
        workflow = self._load_workflow()
        workflow = self._inject_prompt(workflow, prompt, negative_prompt or "")
        client_id = str(uuid.uuid4())

        # Submit the prompt to ComfyUI
        prompt_id = await self._submit_prompt(workflow, client_id)

        # Monitor via WebSocket; fall back to HTTP polling on failure
        ws_connected = False
        try:
            import websockets  # noqa: PLC0415

            async with websockets.connect(self._ws_url(client_id)) as ws:
                ws_connected = True
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    msg_type = msg.get("type", "")
                    if msg_type == "progress":
                        data = msg.get("data", {})
                        yield {
                            "type": "progress",
                            "step": int(data.get("value", 0)),
                            "total": int(data.get("max", 1)),
                        }
                    elif msg_type == "executing":
                        data = msg.get("data", {})
                        if data.get("prompt_id") == prompt_id and data.get("node") is None:
                            break
        except Exception:
            if not ws_connected:
                # WebSocket not reachable — poll HTTP history until done
                await self._poll_until_done(prompt_id)
            # If WS connected but then failed, try to continue to fetch result

        img_bytes = await self._fetch_result(prompt_id)
        yield {"type": "complete", "bytes": img_bytes}

    # ------------------------------------------------------------------
    # Private HTTP helpers
    # ------------------------------------------------------------------

    async def _submit_prompt(self, workflow: dict[str, Any], client_id: str) -> str:
        body: dict[str, Any] = dict(self.config.custom_json or {})
        body.update({"client_id": client_id, "prompt": workflow})

        async def attempt() -> str:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await client.post(
                    self._http_url("/prompt"),
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise RuntimeError("Invalid ComfyUI /prompt response payload")
                prompt_id = data.get("prompt_id")
                if not isinstance(prompt_id, str):
                    raise RuntimeError("ComfyUI /prompt response missing prompt_id")
                return prompt_id

        return await retry_async(
            attempt, self.config.max_retries, label="ComfyUI prompt submission"
        )

    async def _poll_until_done(self, prompt_id: str) -> None:
        """Poll GET /history/{prompt_id} until the entry appears."""
        import asyncio  # noqa: PLC0415

        deadline = self.config.timeout
        for _ in range(deadline):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(self._http_url(f"/history/{prompt_id}"))
                    if resp.status_code == 200 and resp.json().get(prompt_id):
                        return
            except Exception:
                pass
            await asyncio.sleep(1)
        raise TimeoutError(f"ComfyUI prompt {prompt_id} did not complete within {deadline}s")

    async def _fetch_result(self, prompt_id: str) -> bytes:
        """Retrieve the output image bytes from ComfyUI history."""
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            hist_resp = await client.get(self._http_url(f"/history/{prompt_id}"))
            hist_resp.raise_for_status()
            history: dict[str, Any] = hist_resp.json()

            entry = history.get(prompt_id, {})
            outputs = entry.get("outputs", {})

            filename: str | None = None
            subfolder: str = ""
            image_type: str = "output"
            for node_output in outputs.values():
                images = node_output.get("images", [])
                if images:
                    filename = images[0]["filename"]
                    subfolder = images[0].get("subfolder", "")
                    image_type = images[0].get("type", "output")
                    break

            if filename is None:
                raise RuntimeError(f"No output image found for prompt {prompt_id}")

            params: dict[str, str] = {"filename": filename, "type": image_type}
            if subfolder:
                params["subfolder"] = subfolder

            img_resp = await client.get(self._http_url("/view"), params=params)
            img_resp.raise_for_status()
            return img_resp.content

    # ------------------------------------------------------------------
    # Workflow listing
    # ------------------------------------------------------------------

    @staticmethod
    def list_builtin_workflows() -> list[str]:
        """Names of workflow templates shipped with the package."""
        if not _BUILTIN_WORKFLOWS_DIR.exists():
            return []
        return sorted(p.stem for p in _BUILTIN_WORKFLOWS_DIR.glob("*.json"))

    def list_user_workflows(self) -> list[str]:
        """Names of workflow templates in the user data directory."""
        if not self._workflows_dir.exists():
            return []
        return sorted(p.stem for p in self._workflows_dir.glob("*.json"))

    def list_all_workflows(self) -> list[str]:
        """All available workflow names (user overrides first, then built-in)."""
        names: dict[str, None] = {}
        for n in self.list_user_workflows():
            names[n] = None
        for n in self.list_builtin_workflows():
            names.setdefault(n, None)
        return list(names)
