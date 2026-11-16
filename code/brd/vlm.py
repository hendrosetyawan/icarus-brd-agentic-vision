"""Client for an OpenAI-compatible vision-language model server (vLLM) and the tool-using agent loop.

The agent is a plan-act-check loop: each turn the model returns JSON
{"thought", "action", "args"}; we run the tool it chose (zoom, look back/forward
in time, other cameras, reference sheet, track history) and send back what the
tool saw. When it answers ("final"), deterministic self-checks run; failures go
back to the model to investigate and revise. Every episode is saved as a trace.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import requests
from PIL import Image

from .common import fit_width, jpeg_b64, write_json


class VLM:
    """Requests go round-robin to `url` plus any extra servers registered as "host:port" files in
    $VLM_SERVERS_DIR (written by hprc/vlm_server.slurm jobs on other GPUs)."""

    def __init__(self, url: str, model: str = "", timeout: int = 300):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.extra_dir = os.environ.get("VLM_SERVERS_DIR")
        self._urls, self._listed, self._n, self._bad = [self.url], 0.0, 0, {}
        self._lock = threading.Lock()
        self.model = model or self._served_model()

    def _served_model(self) -> str:
        r = requests.get(f"{self.url}/models", timeout=30)
        r.raise_for_status()
        return r.json()["data"][0]["id"]

    def _next_url(self) -> str:
        with self._lock:
            if self.extra_dir and time.time() - self._listed > 60:
                self._listed = time.time()
                extra = [f"http://{p.read_text().strip()}/v1" for p in sorted(Path(self.extra_dir).glob("*"))]
                self._urls = [u for u in [self.url] + extra if time.time() - self._bad.get(u, 0) > 300] or [self.url]
            self._n += 1
            return self._urls[self._n % len(self._urls)]

    def _drop(self, url: str) -> None:
        with self._lock:
            self._bad[url] = time.time()
            if url in self._urls and len(self._urls) > 1:
                self._urls.remove(url)

    def chat(self, messages: list, schema: dict | None = None, max_tokens: int = 700) -> dict:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.0,
                "chat_template_kwargs": {"enable_thinking": False}}  # Qwen3.5 thinks by default; we want direct JSON
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        last = None
        for attempt in range(3):
            url = self._next_url()
            try:
                r = requests.post(f"{url}/chat/completions", json=body, timeout=self.timeout)
                r.raise_for_status()
                return parse_json(r.json()["choices"][0]["message"]["content"] or "")
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                self._drop(url)  # server gone (job ended): stop sending to it for 5 min
            except (requests.RequestException, ValueError, KeyError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"VLM call failed after 3 tries: {last}")


def wait_for_server(url: str, minutes: float = 30) -> None:
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            if requests.get(f"{url.rstrip('/')}/models", timeout=10).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(10)
    raise SystemExit(f"VLM server at {url} did not come up within {minutes} min")


def parse_json(text: str) -> dict:
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    start = text.find("{")
    if start < 0:
        raise ValueError(f"No JSON object in model reply: {text[:200]!r}")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def image_part(img: Image.Image) -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64(img)}"}}


def user(text: str, images=()) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}] + [image_part(i) for i in images]}


def system(text: str) -> dict:
    return {"role": "system", "content": text}


# --------------------------------------------------------------- agent loop
def step_schema(actions: list[str]) -> dict:
    return {"type": "object",
            "properties": {"thought": {"type": "string"},
                           "action": {"type": "string", "enum": actions + ["final"]},
                           "args": {"type": "object"}},
            "required": ["thought", "action", "args"]}


class Agent:
    """tools: {name: (description, fn)}; fn(**args) -> (observation_text, [PIL images]).
    verify(answer) -> list of problems (empty = passed)."""

    def __init__(self, vlm: VLM, system_prompt: str, tools: dict, verify=None,
                 max_steps: int = 8, max_images: int = 6, max_checks: int = 2):
        self.vlm, self.tools, self.verify = vlm, tools, verify
        self.max_steps, self.max_images, self.max_checks = max_steps, max_images, max_checks
        doc = "\n".join(f"- {name}: {desc}" for name, (desc, _) in tools.items())
        self.system_prompt = (
            f"{system_prompt}\n\nYou work step by step. Each turn reply with JSON "
            '{"thought": "...", "action": "<tool or final>", "args": {...}}.\n'
            f"Tools:\n{doc}\n- final: give your answer in args (format given in the task). "
            "Use tools whenever you are not sure; your final answer is checked automatically.")

    def _pruned(self, msgs: list) -> list:
        """Keep the task images and the most recent tool images; drop older ones to stay in context."""
        spots = [(i, j) for i, m in enumerate(msgs) if i > 1 and isinstance(m["content"], list)
                 for j, part in enumerate(m["content"]) if part["type"] == "image_url"]
        keep = self._keep
        drop = set(spots[:-keep]) if len(spots) > keep else set()
        if not drop:
            return msgs
        out = []
        for i, m in enumerate(msgs):
            if isinstance(m["content"], list) and any((i, j) in drop for j in range(len(m["content"]))):
                m = {**m, "content": [{"type": "text", "text": "[earlier image omitted]"} if (i, j) in drop else p
                                      for j, p in enumerate(m["content"])]}
            out.append(m)
        return out

    def run(self, task: str, images: list, meta: dict, trace_dir: Path | None = None):
        msgs = [system(self.system_prompt), user(task, images)]
        # The server rejects requests with more than 8 images (--limit-mm-per-prompt), task images included.
        self._keep = max(1, min(self.max_images, 8 - len(images)))
        trace = {**meta, "task": task, "steps": [], "status": "unresolved", "answer": None}
        t0, checks = time.time(), 0
        for i in range(self.max_steps):
            last = i >= self.max_steps - 2  # last two turns: only "final" allowed, so episodes end with an answer
            if i == self.max_steps - 2:
                msgs.append(user("You are out of tool steps: give your best final answer now."))
            try:
                reply = self.vlm.chat(self._pruned(msgs), schema=step_schema([] if last else list(self.tools)),
                                      max_tokens=2048)  # a 10-cow answer overflows 700 tokens
            except RuntimeError as e:
                trace["error"] = str(e)
                break
            act, args = reply.get("action"), reply.get("args") or {}
            step = {"thought": reply.get("thought", ""), "action": act, "args": args, "images": []}
            msgs.append({"role": "assistant", "content": json.dumps(reply)})
            trace["steps"].append(step)
            if act == "final":
                try:
                    problems = self.verify(args) if self.verify else []
                except Exception as e:  # a malformed answer is a failed check, not a crash
                    problems = [f"Answer format error ({e}); follow the format in the task exactly."]
                step["self_check"] = problems or "passed"
                trace["answer"] = args
                if not problems:
                    trace["status"] = "success"
                    break
                checks += 1
                if checks > self.max_checks:
                    trace["status"] = "failed_check"
                    break
                msgs.append(user("Self-check failed:\n- " + "\n- ".join(problems) +
                                 "\nInvestigate with the tools if needed, then give a corrected final answer."))
                continue
            name, fn = act, self.tools.get(act, (None, None))[1]
            try:
                text, imgs = fn(**args) if fn else (f"Unknown action {name!r}.", [])
            except Exception as e:  # tool errors go back to the model as observations
                text, imgs = f"Tool error: {e}", []
            step["observation"], step["images"] = text, imgs
            msgs.append(user(f"Observation from {name}: {text}", imgs))
        trace["seconds"] = round(time.time() - t0, 1)
        trace["n_steps"] = len(trace["steps"])
        if trace_dir:
            save_trace(trace, images, trace_dir)
        return (trace["answer"] if trace["status"] != "unresolved" else None), trace


def save_trace(trace: dict, task_images: list, out: Path) -> None:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    record = {k: v for k, v in trace.items() if k != "steps"}
    record["task_images"] = []
    for i, im in enumerate(task_images):
        fit_width(im, 768).save(out / f"task_{i}.jpg", quality=80)
        record["task_images"].append(f"task_{i}.jpg")
    record["steps"] = []
    for s, step in enumerate(trace["steps"]):
        names = []
        for j, im in enumerate(step.get("images", [])):
            fit_width(im, 640).save(out / f"step{s}_{j}.jpg", quality=80)
            names.append(f"step{s}_{j}.jpg")
        record["steps"].append({**{k: v for k, v in step.items() if k != "images"}, "images": names})
    write_json(out / "trace.json", record)
